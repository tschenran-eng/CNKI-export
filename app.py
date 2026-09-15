from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import re
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from PySide6.QtCore import Qt, QTimer, QUrl
    from PySide6.QtGui import QDesktopServices, QFont
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QFileDialog,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSpinBox,
        QSplitter,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:
    raise SystemExit(
        "\n缺少 Qt 运行库。\n"
        "请双击同目录下的 install_and_start_cnki_qt.bat，"
        "或执行：\n\n"
        "python -m pip install PySide6-Essentials\n"
    ) from exc

from cnki_metadata_exporter.native_export import CNKINativeExporter
from cnki_metadata_exporter.exporter import CaptchaPending
from playwright.async_api import async_playwright

APP_VERSION = "0.5.0-qt"
DEFAULT_QUERY = "SU=('认知语言学')"
CNKI_RESULT_CAP = 6000
CNKI_PAGE_CAP = 120

FIELD_DEFS: dict[str, tuple[str, tuple[str, ...]]] = {
    "title": ("题名", ("题名", "标题", "title")),
    "pubtime": ("发表时间", ("发表时间", "出版日期", "出版时间", "pubtime", "publicationdate", "date")),
    "keywords": ("关键词", ("关键词", "关键字", "keyword")),
    "abstract": ("摘要", ("摘要", "summary", "abstract")),
    "authors": ("作者", ("作者", "author")),
    "source": ("文献来源", ("文献来源", "来源", "source")),
    "affiliation": ("单位/机构", ("单位", "机构", "affiliation", "institution", "organization")),
    "fund": ("基金", ("基金", "fund")),
    "doi": ("DOI", ("doi",)),
    "url": ("URL/链接", ("url", "链接", "link")),
}
DEFAULT_FIELDS = {"title", "pubtime", "keywords", "abstract"}


def norm_field_text(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[-_:/：；;（）()\[\]【】<>]+", "", value)
    return value


def infer_year_range_from_query(query: str) -> tuple[int | None, int | None]:
    """Best-effort parser for common CNKI YE expressions."""
    q = query or ""
    m = re.search(
        r"\bYE\s+BETWEEN\s*\(\s*['\"]?(\d{4})['\"]?\s*,\s*['\"]?(\d{4})['\"]?\s*\)",
        q,
        re.I,
    )
    if m:
        a, b = map(int, m.groups())
        return min(a, b), max(a, b)

    exact = re.findall(r"\bYE\s*=\s*['\"]?(\d{4})['\"]?", q, re.I)
    if len(set(exact)) == 1 and exact:
        y = int(exact[0])
        return y, y

    lo = None
    hi = None
    for op, y_s in re.findall(r"\bYE\s*(>=|>|<=|<)\s*['\"]?(\d{4})['\"]?", q, re.I):
        y = int(y_s)
        if op == ">=":
            lo = y if lo is None else max(lo, y)
        elif op == ">":
            lo = y + 1 if lo is None else max(lo, y + 1)
        elif op == "<=":
            hi = y if hi is None else min(hi, y)
        elif op == "<":
            hi = y - 1 if hi is None else min(hi, y - 1)
    return lo, hi


def year_shard_query(base_query: str, year: int) -> str:
    return f"({base_query.strip()}) AND YE='{year}'"


class RobustNativeExporter(CNKINativeExporter):
    def __init__(self, root: Path, profile_dir: Path, ui_queue: queue.Queue[tuple[str, Any]]) -> None:
        super().__init__(root, profile_dir, headless=False)
        self.ui_queue = ui_queue
        self.stop_requested = False
        self.export_fields: set[str] = set(DEFAULT_FIELDS)

    async def launch(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        common = dict(
            headless=False,
            accept_downloads=True,
            ignore_https_errors=True,
            locale="zh-CN",
            viewport={"width": 1440, "height": 960},
            args=[
                "--disable-dev-shm-usage",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        )
        errors = []
        for channel, label in (("msedge", "Microsoft Edge"), ("chrome", "Google Chrome")):
            try:
                self.context = await self._playwright.chromium.launch_persistent_context(
                    str(self.profile_dir), channel=channel, **common
                )
                self.log(f"已使用本机 {label} 启动持久化浏览器会话。")
                return
            except Exception as exc:
                errors.append(f"{label}: {exc}")
        await self._playwright.stop()
        raise RuntimeError("未能启动本机 Edge/Chrome。" + " | ".join(errors))

    def emit(self, kind: str, payload: Any) -> None:
        self.ui_queue.put((kind, payload))

    def log(self, message: str) -> None:
        super().log(message)
        self.emit("log", message)

    async def check_stop(self) -> None:
        if self.stop_requested:
            raise asyncio.CancelledError()

    async def wait_for_human_verification(self, page, timeout_minutes: int = 60) -> None:
        if not await self.captcha_visible(page):
            return
        self.emit("status", "等待安全验证")
        self.log("检测到 CNKI 安全验证，请在浏览器中手动完成。验证结束后程序会继续。")
        deadline = asyncio.get_running_loop().time() + timeout_minutes * 60
        while asyncio.get_running_loop().time() < deadline:
            await self.check_stop()
            await page.wait_for_timeout(1000)
            if not await self.captcha_visible(page):
                self.log("安全验证完成，继续执行。")
                self.emit("status", "安全验证完成")
                return
        raise CaptchaPending("等待人工验证超时；已完成批次不会丢失。")

    async def next_page(self, page):
        await self.check_stop()
        result = await super().next_page(page)
        await self.check_stop()
        return result

    async def _wait_grid_ready(self, page, expected_rows: int | None = None, timeout_ms: int = 25000) -> int:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        stable_hits = 0
        last_signature = None
        while asyncio.get_running_loop().time() < deadline:
            await self.check_stop()
            await self.wait_for_human_verification(page)
            rows = await self.current_row_count(page)
            master_count = await page.locator("#selectCheckAll1, input[type='checkbox'][id*='selectCheckAll']").count()
            counter_count = await page.locator("#selectCount").count()
            current_page, _ = await self.page_info(page)
            rows_ok = rows > 0 and (expected_rows is None or rows == expected_rows)
            controls_ok = master_count > 0 and counter_count > 0
            signature = (rows, master_count, counter_count, current_page)
            if rows_ok and controls_ok and signature == last_signature:
                stable_hits += 1
            else:
                stable_hits = 0
            last_signature = signature
            if stable_hits >= 2:
                return rows
            await page.wait_for_timeout(250)
        rows = await self.current_row_count(page)
        raise RuntimeError(f"结果页未稳定：当前读取到 {rows} 行，或“全选/已选计数”控件尚未加载完成。")

    async def set_page_size_stable(self, page, total: int, size: int = 50):
        pages, rows = await super().set_page_size_stable(page, total, size)
        expected_rows = min(size, total)
        rows = await self._wait_grid_ready(page, expected_rows=expected_rows)
        self.log(f"每页 {size} 条已经稳定；当前页 {rows} 条，全选控件已加载。")
        return pages, rows

    async def _wait_selected(self, page, target: int, timeout_ms: int = 7000) -> int:
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        last = await self.selected_count(page)
        while asyncio.get_running_loop().time() < deadline:
            await self.check_stop()
            await self.wait_for_human_verification(page)
            last = await self.selected_count(page)
            if last >= target:
                return last
            await page.wait_for_timeout(150)
        return last

    async def _try_master_checkbox(self, page, target: int) -> bool:
        candidates = page.locator("#selectCheckAll1, input[type='checkbox'][id*='selectCheckAll']")
        n = await candidates.count()
        order: list[int] = []
        hidden: list[int] = []
        for i in range(n):
            item = candidates.nth(i)
            try:
                (order if await item.is_visible() else hidden).append(i)
            except Exception:
                hidden.append(i)
        order.extend(hidden)
        for idx in order:
            item = candidates.nth(idx)
            try:
                await item.evaluate("""el => { el.checked = false; el.indeterminate = false; el.click(); }""")
            except Exception:
                try:
                    await item.click(force=True, timeout=2500)
                except Exception:
                    continue
            if await self._wait_selected(page, target, 5000) >= target:
                return True
        return False

    async def _try_label(self, page, target: int) -> bool:
        labels = page.locator("label.checkAll, label:has(#selectCheckAll1), label:has(input[id*='selectCheckAll'])")
        for i in range(await labels.count()):
            try:
                await labels.nth(i).evaluate("el => el.click()")
            except Exception:
                continue
            if await self._wait_selected(page, target, 4000) >= target:
                return True
        return False

    async def _try_cnki_handler(self, page, target: int) -> bool:
        try:
            result = await page.evaluate("""() => {
                const el = document.querySelector('#selectCheckAll1') || document.querySelector("input[type='checkbox'][id*='selectCheckAll']");
                if (!el) return 'no-element';
                const jq = window.jQuery || window.$;
                if (jq && jq.fn && typeof jq.fn.filenameClick === 'function') {
                    el.checked = true; el.indeterminate = false; jq(el).filenameClick(); return 'ok';
                }
                return 'no-handler';
            }""")
            if result == "ok":
                return await self._wait_selected(page, target, 5000) >= target
        except Exception:
            pass
        return False

    async def _try_row_checkboxes(self, page, target: int) -> bool:
        self.log("顶部全选没有生效，切换为逐行勾选备用方式。")
        try:
            clicked = await page.evaluate("""() => {
                const containers = [...document.querySelectorAll('table.result-table-list, .result-table-list')];
                let rows = [];
                for (const c of containers) {
                    const r = [...c.querySelectorAll('tbody tr')];
                    if (r.length > rows.length) rows = r;
                }
                let clicked = 0;
                for (const row of rows) {
                    const boxes = [...row.querySelectorAll("input[type='checkbox']")].filter(e => !String(e.id || '').includes('selectCheckAll'));
                    const cb = boxes[0];
                    if (cb && !cb.checked) { cb.click(); clicked += 1; }
                }
                return clicked;
            }""")
            self.log(f"逐行备用方式触发了 {clicked} 个复选框。")
        except Exception:
            return False
        return await self._wait_selected(page, target, 8000) >= target

    async def _dump_select_failure(self, page, before: int, rows: int, after: int) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = self.logs_dir / f"select-failure-{stamp}"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(folder / "page.png"), full_page=True)
        except Exception:
            pass
        try:
            (folder / "page.html").write_text(await page.content(), encoding="utf-8")
        except Exception:
            pass
        try:
            checkboxes = await page.locator("input[type='checkbox']").evaluate_all("""els => els.map((e, i) => ({
                index: i, id: e.id || '', name: e.name || '', value: e.value || '', checked: !!e.checked,
                visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length), onclick: e.getAttribute('onclick') || '', outerHTML: e.outerHTML.slice(0, 800)
            }))""")
        except Exception:
            checkboxes = []
        (folder / "diagnostics.json").write_text(json.dumps({"url": page.url, "before": before, "rows": rows, "after": after, "checkboxes": checkboxes}, ensure_ascii=False, indent=2), encoding="utf-8")
        return folder

    async def select_current_page(self, page) -> int:
        await self.check_stop()
        rows = await self._wait_grid_ready(page)
        before = await self.selected_count(page)
        target = before + rows
        self.log(f"全选当前页：本页 {rows} 条；已选 {before} → 目标 {target}。")
        for method in (self._try_master_checkbox, self._try_label, self._try_cnki_handler, self._try_row_checkboxes):
            if await method(page, target):
                after = await self.selected_count(page)
                return after - before
        after = await self.selected_count(page)
        folder = await self._dump_select_failure(page, before, rows, after)
        raise RuntimeError(f"当前页全选失败：目标 {target}，实际 {after}。诊断文件：{folder}")

    async def _export_field_catalog(self, export_page):
        selector = "input[name='SELFDEFINE_selfFiledList'], input[name='newdefine_selfFiledList']"
        inputs = export_page.locator(selector)
        catalog = []
        for i in range(await inputs.count()):
            item = inputs.nth(i)
            try:
                meta = await item.evaluate("""el => {
                    const id = el.id || '';
                    const explicit = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                    const wrapping = el.closest('label'); const parent = el.parentElement; const grand = parent && parent.parentElement;
                    const text = [el.value || '', el.title || '', el.getAttribute('data-name') || '', explicit ? explicit.textContent || '' : '', wrapping ? wrapping.textContent || '' : '', parent ? parent.textContent || '' : '', grand ? grand.textContent || '' : ''].join(' | ');
                    return {id, name: el.name || '', value: el.value || '', text: text.trim()};
                }""")
            except Exception:
                meta = {"id": "", "name": "", "value": "", "text": ""}
            meta["index"] = i
            catalog.append(meta)
        return inputs, catalog

    def _field_matches(self, meta: dict[str, Any], key: str) -> bool:
        combined = norm_field_text(" ".join(str(meta.get(x, "")) for x in ("id", "name", "value", "text")))
        _, aliases = FIELD_DEFS[key]
        return any(norm_field_text(alias) in combined for alias in aliases)

    async def select_all_export_fields(self, export_page) -> None:
        selector = "input[name='SELFDEFINE_selfFiledList'], input[name='newdefine_selfFiledList']"
        inputs = export_page.locator(selector)
        for attempt in range(1, 5):
            if await inputs.count() >= 4:
                break
            side_link = export_page.locator("a[displaymode='selfDefine']").first
            if not await side_link.count():
                side_link = export_page.get_by_text("自定义", exact=True).last
            if await side_link.count():
                try:
                    await side_link.click(force=True)
                except Exception:
                    await side_link.evaluate("e => e.click()")
            await export_page.wait_for_timeout(attempt * 1000)
        if await inputs.count() < 4:
            raise RuntimeError("自定义导出字段未完整加载。")
        inputs, catalog = await self._export_field_catalog(export_page)
        for i in range(await inputs.count()):
            item = inputs.nth(i)
            try:
                if await item.is_checked():
                    await item.uncheck(force=True)
            except Exception:
                try:
                    await item.evaluate("el => { el.checked = false; }")
                except Exception:
                    pass
        matched: dict[str, list[int]] = {key: [] for key in self.export_fields}
        for meta in catalog:
            for key in self.export_fields:
                if self._field_matches(meta, key):
                    matched[key].append(int(meta["index"]))
        missing = [key for key, indices in matched.items() if not indices]
        if missing:
            readable_missing = [FIELD_DEFS[k][0] for k in missing]
            available = [f"{m.get('index')}: {m.get('value','')} | {m.get('text','')[:120]}" for m in catalog]
            diag = self.logs_dir / "export-field-catalog.txt"
            diag.write_text("\n".join(available), encoding="utf-8")
            raise RuntimeError("未能在知网自定义导出页匹配这些字段：" + "、".join(readable_missing) + f"。字段目录已保存：{diag}")
        selected_indices = sorted({idx for indices in matched.values() for idx in indices})
        for idx in selected_indices:
            item = inputs.nth(idx)
            try:
                await item.check(force=True)
            except Exception:
                await item.evaluate("""el => { el.checked = true; el.dispatchEvent(new Event('change', {bubbles: true})); }""")
        checked = await inputs.evaluate_all("els => els.map((e,i) => e.checked ? i : -1).filter(i => i >= 0)")
        checked_set = {int(x) for x in checked}
        if not set(selected_indices).issubset(checked_set):
            raise RuntimeError(f"自定义导出字段勾选未完全生效：目标 {len(selected_indices)}，实际 {len(checked_set)}。")
        readable = [FIELD_DEFS[k][0] for k in sorted(self.export_fields)]
        self.log("本批导出字段：" + "、".join(readable))


class BrowserBackend:
    def __init__(self, ui_queue: queue.Queue[tuple[str, Any]]) -> None:
        self.ui_queue = ui_queue
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.exporter: RobustNativeExporter | None = None
        self.workspace: Path | None = None
        self.browser_ready = False
        self.running = False

    def emit(self, kind: str, payload: Any) -> None:
        self.ui_queue.put((kind, payload))

    def ensure_loop(self) -> None:
        if self.thread and self.thread.is_alive() and self.loop:
            return
        ready = threading.Event()
        def runner():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            ready.set()
            self.loop.run_forever()
        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()
        ready.wait(timeout=5)

    def submit(self, coro):
        self.ensure_loop()
        if not self.loop:
            raise RuntimeError("后台事件循环未启动。")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def open_browser_async(self, workspace: Path) -> None:
        try:
            if self.browser_ready and self.exporter and self.workspace == workspace:
                self.emit("log", "浏览器已经在运行，继续复用当前会话。")
                return
            if self.exporter:
                try:
                    await self.exporter.close()
                except Exception:
                    pass
            workspace.mkdir(parents=True, exist_ok=True)
            self.workspace = workspace
            self.exporter = RobustNativeExporter(workspace, workspace / ".cnki-profile", self.ui_queue)
            self.emit("status", "正在启动浏览器…")
            await self.exporter.launch()
            self.browser_ready = True
            self.emit("browser", True)
            self.emit("status", "浏览器已启动，请完成 CNKI / CARSI / 学校图书馆登录")
            self.emit("log", f"持久化浏览器 profile：{workspace / '.cnki-profile'}")
        except Exception as exc:
            self.browser_ready = False
            self.emit("browser", False)
            self.emit("error", f"浏览器启动失败：{exc}\n\n{traceback.format_exc()}")

    async def check_login_async(self) -> None:
        if not self.exporter or not self.exporter.context:
            self.emit("error", "请先打开浏览器。")
            return
        try:
            context = self.exporter.context
            page = context.pages[0] if context.pages else await context.new_page()
            if "cnki.net" not in page.url:
                await page.goto("https://kns.cnki.net/kns8s/AdvSearch", wait_until="domcontentloaded", timeout=60000)
            await self.exporter.wait_for_human_verification(page)
            unit = page.locator("div.ecp_header_unitName").first
            unit_text = ""
            if await unit.count():
                try:
                    unit_text = (await unit.inner_text()).strip()
                except Exception:
                    pass
            if unit_text:
                self.emit("login", ("ok", unit_text))
                self.emit("status", f"机构访问已确认：{unit_text}")
            else:
                self.emit("login", ("warn", "未确认机构登录"))
                self.emit("status", "未检测到明确机构标记；如页面已有学校权限，可直接运行")
        except Exception as exc:
            self.emit("error", f"检查登录状态失败：{exc}")

    @staticmethod
    def _query_sig(query: str) -> str:
        return hashlib.sha256(query.strip().encode("utf-8")).hexdigest()[:16]

    def guard_resume_state(self, workspace: Path, collection_id: str, query: str, name: str, export_fields: set[str]) -> None:
        native_dir = workspace / "output" / "native_exports" / collection_id
        native_dir.mkdir(parents=True, exist_ok=True)
        state_path = native_dir / "gui_state.json"
        sig = self._query_sig(query)
        if state_path.exists():
            try:
                old = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                old = {}
            old_sig = str(old.get("query_signature", ""))
            old_fields = set(old.get("export_fields", []))
            existing = list(native_dir.glob("*.xls"))
            if existing and old_sig and old_sig != sig:
                raise RuntimeError(f"集合 ID“{collection_id}”已有 {len(existing)} 个旧批次，但当前检索式已改变。请换一个集合 ID，避免把两套结果混在一起。")
            if existing and old_fields and old_fields != export_fields:
                raise RuntimeError(f"集合 ID“{collection_id}”已有 {len(existing)} 个旧批次，但当前导出字段设置与之前不同。请换一个集合 ID，或删除旧批次后重新开始。")
        state_path.write_text(json.dumps({
            "app_version": APP_VERSION, "collection_id": collection_id, "name": name,
            "query": query, "query_signature": sig, "export_fields": sorted(export_fields),
            "updated_at": datetime.now().isoformat(timespec="seconds")
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    async def query_total_async(self, query: str) -> int:
        if not self.exporter or not self.exporter.context:
            raise RuntimeError("浏览器尚未启动。")
        page = self.exporter.context.pages[0] if self.exporter.context.pages else await self.exporter.context.new_page()
        await self.exporter.submit_professional_query(page, query)
        return int(await self.exporter.result_count(page))

    def shard_batch_files(self, workspace: Path, shard_id: str) -> list[Path]:
        folder = workspace / "output" / "native_exports" / shard_id
        return sorted(folder.glob("*.xls")) if folder.exists() else []

    def write_shard_plan(self, workspace: Path, collection_id: str, name: str, base_query: str, export_fields: set[str], year_from: int, year_to: int, shards: list[dict[str, Any]], base_total: int) -> None:
        root = workspace / "output" / "native_exports"
        root.mkdir(parents=True, exist_ok=True)
        plan_path = root / f"{collection_id}__shard_plan.json"
        plan_path.write_text(json.dumps({
            "app_version": APP_VERSION, "collection_id": collection_id, "name": name,
            "base_query": base_query, "base_result_count": base_total, "year_from": year_from,
            "year_to": year_to, "export_fields": sorted(export_fields), "cnki_result_cap": CNKI_RESULT_CAP,
            "shards": shards, "updated_at": datetime.now().isoformat(timespec="seconds")
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    async def run_export_async(self, *, workspace: Path, collection_id: str, name: str, query: str, batch_size: int, max_batches: int | None, export_fields: set[str], year_from: int | None, year_to: int | None) -> None:
        if self.running:
            self.emit("error", "已有任务正在运行。")
            return
        if not self.exporter or not self.exporter.context:
            self.emit("error", "请先打开浏览器并登录。")
            return
        self.running = True
        self.exporter.stop_requested = False
        self.exporter.export_fields = set(export_fields)
        self.emit("running", True)
        self.emit("status", "正在预检索…")
        try:
            base_total = await self.query_total_async(query)
            self.emit("log", f"主检索命中 {base_total} 条。")
            if base_total <= CNKI_RESULT_CAP:
                self.guard_resume_state(workspace, collection_id, query, name, export_fields)
                collection = {"id": collection_id, "name": name, "query": query, "field": query.split("=", 1)[0] if "=" in query else "", "purpose": "Qt GUI", "tier": "GUI"}
                manifest = await self.exporter.export_native_collection(collection, batch_size=batch_size, max_batches=max_batches)
                self.emit("done", {"result_count": manifest.get("result_count", 0), "batches": len(manifest.get("exported_batches", [])), "shards": 1})
                self.emit("status", "任务完成；浏览器保持打开")
                return

            q_lo, q_hi = infer_year_range_from_query(query)
            lo = year_from or q_lo
            hi = year_to or q_hi
            if lo is None or hi is None:
                raise RuntimeError(f"当前检索命中 {base_total} 条，但知网页面单次最多只开放 {CNKI_RESULT_CAP} 条（50 条/页 × {CNKI_PAGE_CAP} 页）。请在 GUI 的“分年导出”中填写起始年和结束年；程序会自动把原检索式按 YE 年份拆分后继续。")
            if lo > hi:
                lo, hi = hi, lo
            if lo < 1900 or hi > 2100:
                raise RuntimeError(f"年份范围不合理：{lo}–{hi}")

            self.emit("log", f"检测到 CNKI {CNKI_RESULT_CAP} 条上限：页面只会给出最多 {CNKI_PAGE_CAP} 页。自动按年份拆分 {lo}–{hi}，每个年份作为独立可续跑分片。")
            self.emit("status", f"正在规划分年导出：{lo}–{hi}")
            shard_plan: list[dict[str, Any]] = []
            year_counts: dict[int, int] = {}
            for year in range(lo, hi + 1):
                await self.exporter.check_stop()
                shard_query = year_shard_query(query, year)
                count = await self.query_total_async(shard_query)
                year_counts[year] = count
                shard_id = f"{collection_id}__Y{year}"
                shard_plan.append({"year": year, "collection_id": shard_id, "query": shard_query, "result_count": count})
                self.emit("log", f"分片预检：{year} 年 = {count} 条。")
                if count > CNKI_RESULT_CAP:
                    raise RuntimeError(f"{year} 年单年仍命中 {count} 条，超过知网 {CNKI_RESULT_CAP} 条上限。该年份需要再按期刊/来源等第二维度拆分；当前版本不会静默漏掉第 6001 条之后的数据。")

            shard_sum = sum(year_counts.values())
            self.write_shard_plan(workspace, collection_id, name, query, export_fields, lo, hi, shard_plan, base_total)
            if shard_sum != base_total:
                self.emit("log", f"注意：分年结果合计 {shard_sum} 条，与主检索 {base_total} 条相差 {shard_sum - base_total:+d}。这通常意味着所填年份范围没有完全覆盖主检索；程序仍按明确年份范围执行，不会把 6000 条上限误当成完整结果。")

            remaining_new_batches = max_batches
            completed_shards = 0
            total_batch_entries = 0
            for idx, shard in enumerate(shard_plan, start=1):
                await self.exporter.check_stop()
                count = int(shard["result_count"])
                if count <= 0:
                    completed_shards += 1
                    continue
                shard_id = str(shard["collection_id"])
                shard_query = str(shard["query"])
                year = int(shard["year"])
                self.guard_resume_state(workspace, shard_id, shard_query, f"{name} · {year}", export_fields)
                before_count = len(self.shard_batch_files(workspace, shard_id))
                if remaining_new_batches is not None and remaining_new_batches <= 0:
                    self.emit("log", "已达到本次“最多批次”限制，停止在下一个分片之前。")
                    break
                shard_max_batches = None if remaining_new_batches is None else before_count + remaining_new_batches
                self.emit("status", f"分年导出 {idx}/{len(shard_plan)}：{year} 年（{count} 条）")
                self.emit("log", f"开始年份分片 {year}：{count} 条；已有 {before_count} 个 XLS 批次。")
                collection = {"id": shard_id, "name": f"{name} · {year}", "query": shard_query, "field": query.split("=", 1)[0] if "=" in query else "", "purpose": f"Qt GUI year shard {year}", "tier": "GUI-YEAR-SHARD"}
                manifest = await self.exporter.export_native_collection(collection, batch_size=batch_size, max_batches=shard_max_batches)
                after_count = len(self.shard_batch_files(workspace, shard_id))
                new_count = max(0, after_count - before_count)
                if remaining_new_batches is not None:
                    remaining_new_batches -= new_count
                total_batch_entries += len(manifest.get("exported_batches", []))
                completed_shards += 1

            self.emit("done", {"result_count": shard_sum, "batches": total_batch_entries, "shards": completed_shards, "base_result_count": base_total})
            self.emit("status", "分年任务完成/已到本次批次限制；浏览器保持打开")
        except asyncio.CancelledError:
            self.emit("log", "任务已停止；已完成批次保留，下次仍可按年份断点续跑。")
            self.emit("status", "任务已停止；浏览器保持打开")
        except Exception as exc:
            self.emit("status", "任务中断；浏览器保持打开")
            self.emit("error", f"任务中断：{exc}\n\n{traceback.format_exc()}")
        finally:
            self.running = False
            self.emit("running", False)

    async def stop_async(self):
        if self.exporter:
            self.exporter.stop_requested = True
            self.emit("status", "正在停止…")

    async def shutdown_async(self):
        if self.exporter:
            self.exporter.stop_requested = True
            try:
                await self.exporter.close()
            except Exception:
                pass
        self.exporter = None
        self.browser_ready = False

    def shutdown(self):
        if not self.loop:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self.shutdown_async(), self.loop)
            fut.result(timeout=8)
        except Exception:
            pass
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass


class Card(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("Card")


class StatusPill(QLabel):
    def __init__(self):
        super().__init__("浏览器未启动")
        self.setProperty("level", "neutral")
        self.setAlignment(Qt.AlignCenter)

    def set_state(self, level: str, text: str):
        self.setProperty("level", level)
        self.setText(text)
        self.style().unpolish(self)
        self.style().polish(self)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.backend = BrowserBackend(self.events)
        self.field_boxes: dict[str, QCheckBox] = {}
        self.setWindowTitle(f"CNKI Metadata Exporter · {APP_VERSION}")
        self.resize(1260, 860)
        self.setMinimumSize(1080, 720)
        self._build()
        self._style()
        self._load_settings()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll_events)
        self.timer.start(120)

    def _build(self):
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(28, 22, 28, 24)
        outer.setSpacing(16)
        head = QHBoxLayout()
        left = QVBoxLayout()
        title = QLabel("CNKI 元数据批量导出")
        title.setObjectName("Title")
        sub = QLabel("持久登录 · 自动分年突破 6000 条限制 · 可选字段 · 批次断点续跑")
        sub.setObjectName("Subtitle")
        left.addWidget(title)
        left.addWidget(sub)
        head.addLayout(left)
        head.addStretch(1)
        self.pill = StatusPill()
        head.addWidget(self.pill)
        outer.addLayout(head)

        session = Card()
        sg = QGridLayout(session)
        sg.setContentsMargins(18, 16, 18, 16)
        sg.setHorizontalSpacing(12)
        sg.setVerticalSpacing(12)
        sec = QLabel("浏览器与登录")
        sec.setObjectName("SectionTitle")
        sg.addWidget(sec, 0, 0, 1, 4)
        sg.addWidget(QLabel("工作目录"), 1, 0)
        self.workspace = QLineEdit()
        sg.addWidget(self.workspace, 1, 1, 1, 2)
        choose = QPushButton("选择目录")
        choose.setProperty("kind", "secondary")
        choose.clicked.connect(self.choose_workspace)
        sg.addWidget(choose, 1, 3)
        self.browser_btn = QPushButton("打开 / 复用浏览器")
        self.browser_btn.setProperty("kind", "primary")
        self.browser_btn.clicked.connect(self.open_browser)
        sg.addWidget(self.browser_btn, 2, 1)
        self.login_btn = QPushButton("检查登录状态")
        self.login_btn.setProperty("kind", "secondary")
        self.login_btn.setEnabled(False)
        self.login_btn.clicked.connect(self.check_login)
        sg.addWidget(self.login_btn, 2, 2)
        sg.setColumnStretch(1, 1)
        sg.setColumnStretch(2, 1)
        outer.addWidget(session)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        query_card = Card()
        qv = QVBoxLayout(query_card)
        qv.setContentsMargins(18, 16, 18, 18)
        qv.setSpacing(12)
        qt = QLabel("检索任务")
        qt.setObjectName("SectionTitle")
        qv.addWidget(qt)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.addWidget(QLabel("集合 ID"), 0, 0)
        self.collection = QLineEdit("Q1")
        grid.addWidget(self.collection, 0, 1)
        grid.addWidget(QLabel("集合名称"), 0, 2)
        self.name = QLineEdit("CNKI 检索")
        grid.addWidget(self.name, 0, 3)
        grid.addWidget(QLabel("每批条数"), 1, 0)
        self.batch = QSpinBox()
        self.batch.setRange(50, 500)
        self.batch.setSingleStep(50)
        self.batch.setValue(500)
        grid.addWidget(self.batch, 1, 1)
        grid.addWidget(QLabel("最多批次"), 1, 2)
        self.max_batches = QSpinBox()
        self.max_batches.setRange(0, 9999)
        self.max_batches.setValue(1)
        self.max_batches.setSpecialValueText("全部")
        grid.addWidget(self.max_batches, 1, 3)
        grid.addWidget(QLabel("分年起始"), 2, 0)
        self.year_from = QSpinBox()
        self.year_from.setRange(0, 2100)
        self.year_from.setSpecialValueText("自动")
        self.year_from.setValue(0)
        self.year_from.setToolTip("当结果超过 6000 条时使用；0=尝试从检索式中的 YE 条件自动识别")
        grid.addWidget(self.year_from, 2, 1)
        grid.addWidget(QLabel("分年结束"), 2, 2)
        self.year_to = QSpinBox()
        self.year_to.setRange(0, 2100)
        self.year_to.setSpecialValueText("自动")
        self.year_to.setValue(0)
        self.year_to.setToolTip("当结果超过 6000 条时使用；0=尝试从检索式中的 YE 条件自动识别")
        grid.addWidget(self.year_to, 2, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        qv.addLayout(grid)
        cap_hint = QLabel("知网单个结果集最多开放 6000 条（50×120 页）。超过 6000 时，程序会按 YE 年份自动拆分；填写起止年，或让程序从检索式中的 YE 条件识别。")
        cap_hint.setObjectName("Hint")
        cap_hint.setWordWrap(True)
        qv.addWidget(cap_hint)
        fl = QLabel("希望导出的字段")
        fl.setObjectName("FieldTitle")
        qv.addWidget(fl)
        field_card = QFrame()
        field_card.setObjectName("FieldBox")
        fg = QGridLayout(field_card)
        fg.setContentsMargins(12, 9, 12, 9)
        fg.setHorizontalSpacing(18)
        fg.setVerticalSpacing(8)
        keys = list(FIELD_DEFS.keys())
        for i, key in enumerate(keys):
            label, _ = FIELD_DEFS[key]
            cb = QCheckBox(label)
            cb.setChecked(key in DEFAULT_FIELDS)
            self.field_boxes[key] = cb
            fg.addWidget(cb, i // 5, i % 5)
        qv.addWidget(field_card)
        hint = QLabel("默认：题名、发表时间、关键词、摘要。字段设置也参与断点一致性检查。")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        qv.addWidget(hint)
        qlabel = QLabel("专业检索式")
        qlabel.setObjectName("FieldTitle")
        qv.addWidget(qlabel)
        self.query = QPlainTextEdit()
        self.query.setObjectName("QueryEditor")
        self.query.setPlainText(DEFAULT_QUERY)
        self.query.setMinimumHeight(190)
        qv.addWidget(self.query, 1)
        self.resume = QLabel("断点状态：尚未检查")
        self.resume.setObjectName("Resume")
        self.resume.setWordWrap(True)
        qv.addWidget(self.resume)
        split.addWidget(query_card)

        ctrl = Card()
        cv = QVBoxLayout(ctrl)
        cv.setContentsMargins(18, 16, 18, 18)
        cv.setSpacing(12)
        ct = QLabel("运行控制")
        ct.setObjectName("SectionTitle")
        cv.addWidget(ct)
        self.progress_title = QLabel("等待开始")
        self.progress_title.setObjectName("ProgressTitle")
        cv.addWidget(self.progress_title)
        self.status = QLabel("浏览器启动后即可运行")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        cv.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        cv.addWidget(self.progress)
        self.start = QPushButton("开始 / 断点续跑")
        self.start.setProperty("kind", "primary")
        self.start.setEnabled(False)
        self.start.clicked.connect(self.start_export)
        cv.addWidget(self.start)
        self.stop = QPushButton("停止当前任务")
        self.stop.setProperty("kind", "danger")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.stop_export)
        cv.addWidget(self.stop)
        outbtn = QPushButton("打开输出目录")
        outbtn.setProperty("kind", "secondary")
        outbtn.clicked.connect(self.open_output)
        cv.addWidget(outbtn)
        cv.addSpacing(8)
        dp = QLabel("断点单位是已成功下载的 XLS 批次。\n\n例如 500 条一批：若在第 5 页中断，重新运行只重做当前批次；此前已经下载成功的批次会直接跳过。")
        dp.setObjectName("Hint")
        dp.setWordWrap(True)
        cv.addWidget(dp)
        cv.addStretch(1)
        split.addWidget(ctrl)
        split.setSizes([800, 380])
        outer.addWidget(split, 1)

        log_card = Card()
        lv = QVBoxLayout(log_card)
        lv.setContentsMargins(14, 12, 14, 14)
        lh = QHBoxLayout()
        ltitle = QLabel("运行日志")
        ltitle.setObjectName("SectionTitle")
        lh.addWidget(ltitle)
        lh.addStretch(1)
        clear = QPushButton("清空日志")
        clear.setProperty("kind", "ghost")
        clear.clicked.connect(lambda: self.log.clear())
        lh.addWidget(clear)
        lv.addLayout(lh)
        self.log = QPlainTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setMinimumHeight(170)
        lv.addWidget(self.log)
        outer.addWidget(log_card)
        self.workspace.textChanged.connect(self.refresh_resume)
        self.collection.textChanged.connect(self.refresh_resume)
        self.query.textChanged.connect(self.refresh_resume)
        for cb in self.field_boxes.values():
            cb.stateChanged.connect(self.refresh_resume)
        self.year_from.valueChanged.connect(self.refresh_resume)
        self.year_to.valueChanged.connect(self.refresh_resume)

    def _style(self):
        self.setStyleSheet("""
        QWidget#Root { background:#F4F6FA; color:#172033; font-family:"Segoe UI","Microsoft YaHei UI"; font-size:14px; }
        QLabel#Title { font-size:28px; font-weight:700; color:#111827; }
        QLabel#Subtitle { color:#738096; font-size:13px; }
        QFrame#Card { background:#FFFFFF; border:1px solid #E4E8F0; border-radius:12px; }
        QFrame#FieldBox { background:#F8FAFC; border:1px solid #E5EAF1; border-radius:9px; }
        QLabel#SectionTitle { font-size:16px; font-weight:650; }
        QLabel#FieldTitle, QLabel#ProgressTitle { font-weight:650; color:#263247; }
        QLabel#Hint { color:#758196; font-size:12px; }
        QLabel#Resume { background:#F7F9FC; color:#5F6C80; border:1px solid #E6EAF1; border-radius:8px; padding:9px 10px; }
        QLineEdit, QSpinBox, QPlainTextEdit#QueryEditor { background:#FBFCFE; color:#172033; border:1px solid #DDE3EC; border-radius:8px; padding:8px 10px; }
        QLineEdit:focus, QSpinBox:focus, QPlainTextEdit#QueryEditor:focus { border:1px solid #4B7CF3; background:#FFFFFF; }
        QPlainTextEdit#QueryEditor { font-family:"Cascadia Mono","Consolas","Microsoft YaHei UI"; font-size:13px; }
        QPushButton { min-height:34px; border-radius:8px; padding:5px 14px; font-weight:600; }
        QPushButton[kind="primary"] { color:white; background:#2F6FEB; border:1px solid #2F6FEB; }
        QPushButton[kind="primary"]:hover { background:#255FCC; }
        QPushButton[kind="primary"]:disabled { background:#AEBBD2; border-color:#AEBBD2; }
        QPushButton[kind="secondary"] { color:#2B3547; background:#F7F9FC; border:1px solid #DDE3EC; }
        QPushButton[kind="secondary"]:hover { background:#EDF2F8; }
        QPushButton[kind="danger"] { color:#B42318; background:#FFF2F0; border:1px solid #FFD7D2; }
        QPushButton[kind="ghost"] { color:#5E6B80; background:transparent; border:1px solid transparent; }
        QProgressBar { background:#EAF0F7; border:none; border-radius:5px; min-height:10px; max-height:10px; }
        QProgressBar::chunk { background:#2F6FEB; border-radius:5px; }
        QPlainTextEdit#Log { background:#111827; color:#D9E2F2; border:1px solid #1F2937; border-radius:9px; padding:8px; font-family:"Cascadia Mono","Consolas","Microsoft YaHei UI"; font-size:12px; }
        QCheckBox { spacing:7px; }
        QCheckBox::indicator { width:17px; height:17px; }
        QLabel[level="neutral"] { color:#536176; background:#EEF2F7; border:1px solid #DEE5EF; border-radius:12px; padding:6px 12px; font-weight:600; }
        QLabel[level="ok"] { color:#067647; background:#ECFDF3; border:1px solid #ABEFC6; border-radius:12px; padding:6px 12px; font-weight:600; }
        QLabel[level="warn"] { color:#B54708; background:#FFFAEB; border:1px solid #FEDF89; border-radius:12px; padding:6px 12px; font-weight:600; }
        QLabel[level="bad"] { color:#B42318; background:#FEF3F2; border:1px solid #FECDCA; border-radius:12px; padding:6px 12px; font-weight:600; }
        """)

    def settings_path(self):
        return Path.home() / ".cnki_metadata_exporter_gui_v05.json"

    def selected_fields(self) -> set[str]:
        return {k for k, cb in self.field_boxes.items() if cb.isChecked()}

    def _load_settings(self):
        p = self.settings_path()
        data = {}
        try:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        self.workspace.setText(data.get("workspace", str(Path.cwd() / "cnki_qt_workspace")))
        self.collection.setText(data.get("collection_id", "Q1"))
        self.name.setText(data.get("name", "CNKI 检索"))
        self.query.setPlainText(data.get("query", DEFAULT_QUERY))
        self.batch.setValue(int(data.get("batch_size", 500)))
        self.max_batches.setValue(int(data.get("max_batches", 1)))
        self.year_from.setValue(int(data.get("year_from", 0)))
        self.year_to.setValue(int(data.get("year_to", 0)))
        saved_fields = set(data.get("export_fields", list(DEFAULT_FIELDS)))
        for k, cb in self.field_boxes.items():
            cb.setChecked(k in saved_fields)
        self.refresh_resume()

    def save_settings(self):
        try:
            self.settings_path().write_text(json.dumps({
                "workspace": self.workspace.text().strip(), "collection_id": self.collection.text().strip(),
                "name": self.name.text().strip(), "query": self.query.toPlainText().strip(),
                "batch_size": self.batch.value(), "max_batches": self.max_batches.value(),
                "year_from": self.year_from.value(), "year_to": self.year_to.value(),
                "export_fields": sorted(self.selected_fields())
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def workspace_path(self) -> Path:
        value = self.workspace.text().strip()
        if not value:
            raise ValueError("工作目录不能为空。")
        return Path(value).expanduser().resolve()

    def append_log(self, text: str):
        self.log.appendPlainText(f"[{datetime.now():%H:%M:%S}] {text}")
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())
        m = re.search(r"批次\s+(\d+)/(\d+).*第\s+(\d+)/(\d+)\s+页.*已选\s+(\d+)", text)
        if m:
            b, bt, p, pt, sel = map(int, m.groups())
            self.progress_title.setText(f"批次 {b} / {bt}")
            self.status.setText(f"结果页 {p} / {pt} · 当前批次已选 {sel} 条")
            self.progress.setValue(min(99, round(sel / max(1, self.batch.value()) * 100)))

    def poll_events(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append_log(str(payload))
            elif kind == "status":
                self.status.setText(str(payload))
            elif kind == "browser":
                ready = bool(payload)
                self.login_btn.setEnabled(ready)
                self.start.setEnabled(ready and not self.backend.running)
                self.browser_btn.setEnabled(True)
                self.pill.set_state("warn" if ready else "neutral", "浏览器已启动" if ready else "浏览器未启动")
            elif kind == "login":
                level, text = payload
                self.pill.set_state(level, str(text))
            elif kind == "running":
                running = bool(payload)
                self.start.setEnabled(self.backend.browser_ready and not running)
                self.stop.setEnabled(running)
            elif kind == "done":
                self.progress.setValue(100)
                self.progress_title.setText("任务完成")
                self.status.setText(f"命中/分片合计 {payload.get('result_count', 0)} 条；处理 {payload.get('shards', 1)} 个分片；记录 {payload.get('batches', 0)} 个批次条目")
                self.refresh_resume()
            elif kind == "error":
                self.append_log(str(payload))
                QMessageBox.critical(self, "运行错误", str(payload))

    def choose_workspace(self):
        folder = QFileDialog.getExistingDirectory(self, "选择工作目录", self.workspace.text().strip() or str(Path.home()))
        if folder:
            self.workspace.setText(folder)

    def open_browser(self):
        try:
            wp = self.workspace_path()
        except Exception as exc:
            QMessageBox.warning(self, "配置错误", str(exc))
            return
        self.save_settings()
        self.browser_btn.setEnabled(False)
        self.pill.set_state("warn", "正在启动…")
        self.backend.submit(self.backend.open_browser_async(wp))

    def check_login(self):
        self.backend.submit(self.backend.check_login_async())

    def start_export(self):
        try:
            wp = self.workspace_path()
            cid = self.collection.text().strip()
            name = self.name.text().strip() or cid
            query = self.query.toPlainText().strip()
            fields = self.selected_fields()
            if not cid:
                raise ValueError("集合 ID 不能为空。")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", cid):
                raise ValueError("集合 ID 只能使用英文字母、数字、点、下划线和连字符。")
            if not query:
                raise ValueError("专业检索式不能为空。")
            if not fields:
                raise ValueError("至少选择一个希望导出的字段。")
            if self.batch.value() % 50 != 0:
                raise ValueError("每批条数必须是 50 的整数倍。")
            max_batches = None if self.max_batches.value() == 0 else self.max_batches.value()
            year_from = None if self.year_from.value() == 0 else self.year_from.value()
            year_to = None if self.year_to.value() == 0 else self.year_to.value()
            if (year_from is None) ^ (year_to is None):
                raise ValueError("分年起始和结束要么都留为“自动”，要么都填写。")
            if year_from is not None and year_to is not None and year_from > year_to:
                year_from, year_to = year_to, year_from
        except Exception as exc:
            QMessageBox.warning(self, "配置错误", str(exc))
            return
        self.save_settings()
        self.progress.setValue(0)
        self.progress_title.setText("准备运行")
        self.backend.submit(self.backend.run_export_async(
            workspace=wp, collection_id=cid, name=name, query=query, batch_size=self.batch.value(),
            max_batches=max_batches, export_fields=fields, year_from=year_from, year_to=year_to
        ))

    def stop_export(self):
        self.backend.submit(self.backend.stop_async())

    def open_output(self):
        try:
            root = self.workspace_path() / "output" / "native_exports"
            cid = self.collection.text().strip() or "Q1"
            normal = root / cid
            shards = list(root.glob(f"{cid}__Y*")) if root.exists() else []
            path = root if shards else normal
            path.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as exc:
            QMessageBox.warning(self, "无法打开目录", str(exc))

    def refresh_resume(self):
        try:
            wp = self.workspace_path()
            cid = self.collection.text().strip()
            if not cid:
                self.resume.setText("断点状态：请填写集合 ID。")
                return
            root = wp / "output" / "native_exports"
            folder = root / cid
            files = sorted(folder.glob("*.xls")) if folder.exists() else []
            shard_dirs = sorted(p for p in root.glob(f"{cid}__Y*") if p.is_dir()) if root.exists() else []
            shard_files = [f for d in shard_dirs for f in d.glob("*.xls")]
            all_files = files + shard_files
            if not all_files:
                self.resume.setText("断点状态：没有已完成批次，将从第 1 批开始。")
                return
            state_path = folder / "gui_state.json"
            warnings = []
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    query_sig = hashlib.sha256(self.query.toPlainText().strip().encode("utf-8")).hexdigest()[:16]
                    if state.get("query_signature") and state.get("query_signature") != query_sig:
                        warnings.append("检索式已改变")
                    old_fields = set(state.get("export_fields", []))
                    if old_fields and old_fields != self.selected_fields():
                        warnings.append("导出字段已改变")
                except Exception:
                    pass
            if warnings:
                self.resume.setText(f"断点警告：已有 {len(all_files)} 个 XLS 批次，但" + "、".join(warnings) + "。请换集合 ID，避免混用旧数据。")
            else:
                self.resume.setText(f"断点状态：检测到 {len(all_files)} 个已完成 XLS 批次（含 {len(shard_dirs)} 个年份分片），继续运行时会自动跳过。")
        except Exception:
            self.resume.setText("断点状态：暂时无法读取。")

    def closeEvent(self, event):
        if self.backend.running:
            ans = QMessageBox.question(self, "退出", "当前任务仍在运行。已完成批次会保留。\n确定退出吗？", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans != QMessageBox.Yes:
                event.ignore()
                return
        self.save_settings()
        self.backend.shutdown()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("CNKI Metadata Exporter")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
