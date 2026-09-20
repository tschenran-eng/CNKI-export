from __future__ import annotations

import asyncio
import json
import math
import re
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import app_responsive

app = app_responsive.app
app.APP_VERSION = "0.9.0-multi-account"


class WorkerQueue:
    def __init__(self, target, worker_no: int):
        self.target = target
        self.worker_no = worker_no

    def put(self, item):
        try:
            kind, payload = item
        except Exception:
            self.target.put(item)
            return
        if kind == "log":
            payload = f"[W{self.worker_no}] {payload}"
        elif kind == "status":
            payload = f"W{self.worker_no}: {payload}"
        self.target.put((kind, payload))


class WorkerExporter(app.RobustNativeExporter):
    """Exporter bound to one browser/account and one fixed result page."""

    def __init__(
        self,
        root: Path,
        profile_dir: Path,
        ui_queue,
        worker_no: int,
    ):
        super().__init__(
            root,
            profile_dir,
            WorkerQueue(ui_queue, worker_no),
        )
        self.worker_no = worker_no
        self.bound_page = None

    async def launch(self) -> None:
        await super().launch()
        if not self.context:
            raise RuntimeError("浏览器 context 未创建")
        self.bound_page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )
        self.log(
            f"Worker {self.worker_no} 已绑定独立浏览器和固定结果页；"
            "后续检索不会借用其他 Worker 的页面。"
        )

    async def open_custom_export(self, page):
        """Open export popup from THIS worker page/context only."""
        if page.context is not self.context:
            raise RuntimeError(
                f"W{self.worker_no} 页面归属检查失败：检测到页面来自其他浏览器 context"
            )

        link = page.locator("a[exporttype='selfDefine']").first
        if not await link.count():
            menu = page.get_by_text("导出与分析", exact=False).first
            if await menu.count():
                await menu.click(force=True)
            link = page.locator("a[exporttype='selfDefine']").first
        if not await link.count():
            raise RuntimeError("未找到知网自定义导出入口")

        export_page = None
        try:
            async with page.expect_popup(timeout=20000) as popup_info:
                try:
                    await link.click(force=True)
                except Exception:
                    await link.evaluate("e => e.click()")
            export_page = await popup_info.value
        except Exception:
            # Only inspect pages from THIS worker's own browser context.
            candidates = [
                p
                for p in self.context.pages
                if "dm8/manage/export.html" in p.url
            ]
            if not candidates:
                raise RuntimeError("自定义导出页面未在当前 Worker 浏览器中打开")
            export_page = candidates[-1]

        if export_page.context is not self.context:
            raise RuntimeError(
                f"W{self.worker_no} 导出页错误地进入了其他浏览器 context"
            )

        await export_page.wait_for_load_state(
            "domcontentloaded", timeout=60000
        )
        await self.wait_for_human_verification(export_page)
        await export_page.locator(
            "#litoexcel, .export-sidebar-a"
        ).first.wait_for(state="visible", timeout=30000)

        fields = export_page.locator(
            "input[name='SELFDEFINE_selfFiledList'], "
            "input[name='newdefine_selfFiledList']"
        )
        if await fields.count() < 4:
            side_link = export_page.locator(
                "a[displaymode='selfDefine']"
            ).first
            if not await side_link.count():
                side_link = export_page.get_by_text(
                    "自定义", exact=True
                ).last
            if await side_link.count():
                try:
                    await side_link.click(force=True)
                except Exception:
                    await side_link.evaluate("e => e.click()")
                await fields.first.wait_for(
                    state="visible", timeout=30000
                )
        return export_page

    async def export_native_collection_bound(
        self,
        collection: dict[str, Any],
        *,
        batch_size: int = 500,
        max_batches: int | None = None,
    ) -> dict[str, Any]:
        """Page-bound copy of native export.

        The upstream implementation uses context.pages[0]. In multi-account
        mode that is deliberately forbidden: each worker keeps one fixed page
        created inside its own persistent browser context.
        """
        if self.context is None or self.bound_page is None:
            raise RuntimeError("Worker 浏览器尚未启动")

        page = self.bound_page
        if page.context is not self.context:
            raise RuntimeError("Worker 固定页面 context 不匹配")

        cid = str(collection["id"])
        native_dir = self.output_dir / "native_exports" / cid
        native_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = native_dir / "manifest.json"

        self.log(
            f"{cid} 开始导出：{collection['name']} "
            f"(固定浏览器 W{self.worker_no})"
        )
        total, pages, per_page = await self.search_collection(
            page, collection
        )
        self.log(
            f"{cid} 命中 {total} 条，共 {pages} 页，每页 {per_page} 条；"
            f"按每批 {batch_size} 条导出"
        )
        await self.clear_selection(page)

        if total == 0 or per_page == 0:
            manifest = {
                "collection": cid,
                "name": collection["name"],
                "query": collection["query"],
                "result_count": total,
                "pages": pages,
                "batch_size": batch_size,
                "worker": self.worker_no,
                "exported_batches": [],
                "completed_at": datetime.now().isoformat(
                    timespec="seconds"
                ),
            }
            manifest_path.write_text(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return manifest

        pages_per_batch = max(1, batch_size // per_page)
        batch_count = math.ceil(
            pages / pages_per_batch
        ) if pages else 0
        if max_batches is not None:
            batch_count = min(batch_count, max_batches)

        exported: list[dict[str, Any]] = []

        for batch_index in range(batch_count):
            await self.check_stop()
            first_page = batch_index * pages_per_batch + 1
            last_page = min(
                pages,
                first_page + pages_per_batch - 1,
            )
            first_record = (first_page - 1) * per_page + 1
            last_record = min(total, last_page * per_page)
            destination = (
                native_dir
                / f"{cid}_{first_record:05d}-{last_record:05d}.xls"
            )

            if destination.exists() and destination.stat().st_size > 0:
                self.log(
                    f"{cid} 已存在批次 {batch_index + 1}/"
                    f"{batch_count}：{destination.name}"
                )
                exported.append(
                    {
                        "batch": batch_index + 1,
                        "first_record": first_record,
                        "last_record": last_record,
                        "expected_records": (
                            last_record - first_record + 1
                        ),
                        "file": destination.relative_to(
                            self.root
                        ).as_posix(),
                        "bytes": destination.stat().st_size,
                        "status": "existing",
                        "worker": self.worker_no,
                    }
                )
                await self.goto_page_number(page, last_page)
                if last_page < pages:
                    await self.next_page(page)
                continue

            await self.goto_page_number(page, first_page)
            await self.clear_selection(page)
            selected_expected = 0

            for pno in range(first_page, last_page + 1):
                await self.check_stop()
                current, _ = await self.page_info(page)
                if current != pno:
                    raise RuntimeError(
                        f"批次页码异常：应为 {pno}，实际为 {current}"
                    )

                added = await self.select_current_page(page)
                selected_expected += added
                selected_now = await self.selected_count(page)
                self.log(
                    f"{cid} 批次 {batch_index + 1}/{batch_count}："
                    f"第 {pno}/{pages} 页，已选 {selected_now} 条"
                )
                await page.wait_for_timeout(1200)

                if pno < last_page:
                    if not await self.next_page(page):
                        raise RuntimeError(
                            f"{cid} 第 {pno} 页后无法翻页"
                        )

            selected = await self.selected_count(page)
            expected = last_record - first_record + 1
            if (
                selected != expected
                or selected_expected != expected
            ):
                raise RuntimeError(
                    f"{cid} 批次选择数量异常：页面显示 "
                    f"{selected}，累计新增 {selected_expected}，"
                    f"应为 {expected}"
                )

            last_export_error = None
            for export_attempt in range(1, 5):
                await self.check_stop()
                export_page = None
                try:
                    export_page = await self.open_custom_export(
                        page
                    )
                    await self.download_xls(
                        export_page,
                        destination,
                    )
                    last_export_error = None
                    break
                except Exception as exc:
                    last_export_error = exc
                    self.log(
                        f"{cid} 批次 {batch_index + 1} "
                        f"第 {export_attempt} 次 XLS 导出失败："
                        f"{exc}"
                    )
                    await page.wait_for_timeout(
                        export_attempt * 3000
                    )
                finally:
                    if (
                        export_page is not None
                        and not export_page.is_closed()
                    ):
                        try:
                            await export_page.close()
                        except Exception:
                            pass

            if last_export_error is not None:
                raise RuntimeError(
                    f"{cid} 批次 {batch_index + 1} "
                    "多次 XLS 导出失败"
                ) from last_export_error

            exported.append(
                {
                    "batch": batch_index + 1,
                    "first_record": first_record,
                    "last_record": last_record,
                    "expected_records": expected,
                    "file": destination.relative_to(
                        self.root
                    ).as_posix(),
                    "bytes": destination.stat().st_size,
                    "status": "downloaded",
                    "worker": self.worker_no,
                }
            )
            self.log(
                f"{cid} 批次 {batch_index + 1}/"
                f"{batch_count} 已下载：{destination.name}"
            )
            await self.clear_selection(page)

            if last_page < pages:
                await self.next_page(page)

        manifest = {
            "collection": cid,
            "name": collection["name"],
            "query": collection["query"],
            "result_count": total,
            "pages": pages,
            "batch_size": batch_size,
            "worker": self.worker_no,
            "exported_batches": exported,
            "completed_at": datetime.now().isoformat(
                timespec="seconds"
            ),
        }
        manifest_path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.log(
            f"{cid} 原生导出完成：{len(exported)} 个批次"
        )
        return manifest


@dataclass
class WorkerState:
    worker_no: int
    exporter: WorkerExporter
    account_note: str


class MultiAccountBackend(app.BrowserBackend):
    def __init__(self, ui_queue):
        super().__init__(ui_queue)
        self.workers: dict[int, WorkerState] = {}
        self._active_controller_task: asyncio.Task | None = None
        self._active_worker_tasks: list[asyncio.Task] = []

    async def _close_workers(self) -> None:
        states = list(self.workers.values())
        self.workers = {}
        for state in states:
            state.exporter.stop_requested = True
        await asyncio.gather(
            *[
                state.exporter.close()
                for state in states
            ],
            return_exceptions=True,
        )
        self.exporter = None
        self.browser_ready = False

    async def open_workers_async(
        self,
        workspace: Path,
        tasks: list[dict[str, Any]],
    ) -> None:
        try:
            await self._close_workers()
            workspace.mkdir(parents=True, exist_ok=True)
            self.workspace = workspace

            self.emit(
                "status",
                f"正在启动 {len(tasks)} 个独立账户浏览器…",
            )

            states: dict[int, WorkerState] = {}
            for worker_no, task in enumerate(tasks, start=1):
                profile = (
                    workspace
                    / ".cnki-workers"
                    / f"W{worker_no}"
                )
                exporter = WorkerExporter(
                    workspace,
                    profile,
                    self.ui_queue,
                    worker_no,
                )
                self.emit(
                    "status",
                    f"正在启动 Worker {worker_no}/"
                    f"{len(tasks)}…",
                )
                try:
                    await exporter.launch()
                except Exception:
                    for state in states.values():
                        try:
                            await state.exporter.close()
                        except Exception:
                            pass
                    raise

                note = task.get("account_note") or (
                    f"账户 {worker_no}"
                )
                states[worker_no] = WorkerState(
                    worker_no=worker_no,
                    exporter=exporter,
                    account_note=note,
                )

                self.emit(
                    "log",
                    f"[W{worker_no}] 独立 profile：{profile}",
                )
                self.emit(
                    "log",
                    f"[W{worker_no}] 账户备注：{note}",
                )
                self.emit(
                    "log",
                    f"[W{worker_no}] 已固定绑定自己的 browser "
                    "context + main page；不会调用 W1 的 pages[0]。",
                )
                await asyncio.sleep(0.8)

            self.workers = states
            self.exporter = (
                states[1].exporter if states else None
            )
            self.browser_ready = bool(states)
            self.emit("browser", self.browser_ready)
            self.emit(
                "status",
                f"已启动 {len(states)} 个独立浏览器；"
                "请分别登录不同 CNKI 账户。",
            )
        except Exception as exc:
            await self._close_workers()
            self.emit("browser", False)
            self.emit(
                "error",
                "多账户浏览器启动失败："
                f"{exc}\n\n{traceback.format_exc()}",
            )

    async def _check_one_login(
        self,
        state: WorkerState,
    ):
        exporter = state.exporter
        page = exporter.bound_page
        if not page:
            return (
                state.worker_no,
                False,
                "固定页面不存在",
            )

        if page.context is not exporter.context:
            return (
                state.worker_no,
                False,
                "页面 context 错误",
            )

        if "cnki.net" not in page.url:
            await page.goto(
                "https://kns.cnki.net/kns8s/AdvSearch",
                wait_until="domcontentloaded",
                timeout=60000,
            )

        await exporter.wait_for_human_verification(page)

        unit = page.locator(
            "div.ecp_header_unitName"
        ).first
        unit_text = ""
        if await unit.count():
            try:
                unit_text = (
                    await unit.inner_text()
                ).strip()
            except Exception:
                pass

        if unit_text:
            return (
                state.worker_no,
                True,
                unit_text,
            )

        try:
            body = await page.locator(
                "body"
            ).inner_text(timeout=5000)
        except Exception:
            body = ""

        if any(
            x in body
            for x in ("退出", "个人中心", "我的知网")
        ):
            return (
                state.worker_no,
                True,
                "已检测到账户登录",
            )

        return (
            state.worker_no,
            False,
            "未检测到明确机构/账户标记",
        )

    async def check_login_async(self) -> None:
        if not self.workers:
            self.emit(
                "error",
                "请先启动多账户 Worker 浏览器。",
            )
            return

        results = await asyncio.gather(
            *[
                self._check_one_login(state)
                for state in self.workers.values()
            ],
            return_exceptions=True,
        )

        ok = 0
        details = []
        for i, result in enumerate(results, start=1):
            if isinstance(result, Exception):
                details.append(f"W{i}=检查失败")
                continue
            worker_no, success, text = result
            ok += int(success)
            note = self.workers[
                worker_no
            ].account_note
            details.append(
                f"W{worker_no}({note})="
                f"{'✓' if success else '?'} {text}"
            )

        level = (
            "ok"
            if ok == len(self.workers)
            else "warn"
        )
        self.emit(
            "login",
            (
                level,
                f"{ok}/{len(self.workers)} 已确认登录",
            ),
        )
        self.emit(
            "status",
            "；".join(details),
        )
        self.emit(
            "log",
            "多账户登录检查："
            + " | ".join(details),
        )

    async def _run_worker(
        self,
        state: WorkerState,
        task: dict[str, Any],
        *,
        workspace: Path,
        batch_size: int,
        max_batches: int | None,
        export_fields: set[str],
    ) -> dict[str, Any]:
        exporter = state.exporter
        exporter.export_fields = set(export_fields)
        exporter.stop_requested = False

        cid = task["id"]
        name = task["name"]
        query = task["query"]
        year_from = task.get("year_from")
        year_to = task.get("year_to")

        page = exporter.bound_page
        if page is None:
            raise RuntimeError(
                f"W{state.worker_no} 固定结果页不存在"
            )
        if page.context is not exporter.context:
            raise RuntimeError(
                f"W{state.worker_no} 页面进入了其他浏览器"
            )

        exporter.log(
            f"账户={state.account_note}；"
            f"开始任务 {cid} / {name}"
        )

        await exporter.submit_professional_query(
            page,
            query,
        )
        base_total = int(
            await exporter.result_count(page)
        )
        exporter.log(
            f"主检索命中 {base_total} 条。"
        )

        if base_total <= app.CNKI_RESULT_CAP:
            self.guard_resume_state(
                workspace,
                cid,
                query,
                name,
                export_fields,
            )
            collection = {
                "id": cid,
                "name": name,
                "query": query,
                "field": (
                    query.split("=", 1)[0]
                    if "=" in query
                    else ""
                ),
                "purpose": (
                    f"multi-account W{state.worker_no}"
                ),
                "tier": "GUI-MULTI-ACCOUNT",
            }
            return await exporter.export_native_collection_bound(
                collection,
                batch_size=batch_size,
                max_batches=max_batches,
            )

        q_lo, q_hi = app.infer_year_range_from_query(
            query
        )
        lo = year_from or q_lo
        hi = year_to or q_hi

        if lo is None or hi is None:
            raise RuntimeError(
                f"{cid} 命中 {base_total} 条，超过 6000；"
                "请填写这个 Worker 的分年起始/结束。"
            )

        if lo > hi:
            lo, hi = hi, lo

        remaining = max_batches
        manifests = []

        exporter.log(
            f"超过 6000 条；在 W{state.worker_no} "
            f"自己的浏览器中按 {lo}–{hi} 年顺序分片。"
        )

        for year in range(lo, hi + 1):
            await exporter.check_stop()

            shard_query = app.year_shard_query(
                query,
                year,
            )
            shard_id = f"{cid}__Y{year}"

            await exporter.submit_professional_query(
                page,
                shard_query,
            )
            count = int(
                await exporter.result_count(page)
            )
            exporter.log(
                f"分片预检：{year} 年 = {count} 条。"
            )

            if count > app.CNKI_RESULT_CAP:
                raise RuntimeError(
                    f"{cid} / {year} 年仍有 {count} 条，"
                    "单年也超过 6000，需要第二维度拆分。"
                )

            if count <= 0:
                continue

            before = len(
                self.shard_batch_files(
                    workspace,
                    shard_id,
                )
            )

            if (
                remaining is not None
                and remaining <= 0
            ):
                break

            shard_limit = (
                None
                if remaining is None
                else before + remaining
            )

            self.guard_resume_state(
                workspace,
                shard_id,
                shard_query,
                f"{name} · {year}",
                export_fields,
            )

            collection = {
                "id": shard_id,
                "name": f"{name} · {year}",
                "query": shard_query,
                "field": (
                    query.split("=", 1)[0]
                    if "=" in query
                    else ""
                ),
                "purpose": (
                    f"multi-account W{state.worker_no} "
                    f"year {year}"
                ),
                "tier": "GUI-MULTI-ACCOUNT-YEAR",
            }

            manifest = (
                await exporter.export_native_collection_bound(
                    collection,
                    batch_size=batch_size,
                    max_batches=shard_limit,
                )
            )
            manifests.append(manifest)

            if remaining is not None:
                after = len(
                    self.shard_batch_files(
                        workspace,
                        shard_id,
                    )
                )
                remaining -= max(
                    0,
                    after - before,
                )

        return {
            "collection": cid,
            "name": name,
            "query": query,
            "result_count": base_total,
            "worker": state.worker_no,
            "exported_batches": [
                b
                for manifest in manifests
                for b in manifest.get(
                    "exported_batches",
                    [],
                )
            ],
        }

    async def run_workers_async(
        self,
        *,
        workspace: Path,
        tasks: list[dict[str, Any]],
        batch_size: int,
        max_batches: int | None,
        export_fields: set[str],
    ) -> None:
        if self.running:
            self.emit(
                "error",
                "已有多账户任务正在运行。",
            )
            return

        if len(self.workers) != len(tasks):
            self.emit(
                "error",
                "当前已启动 Worker 数量与启用任务数量不一致。"
                "请点击“启动 / 重建 Worker 浏览器”后再运行。",
            )
            return

        self.running = True
        self._active_controller_task = (
            asyncio.current_task()
        )
        self.emit("running", True)
        self.emit(
            "status",
            f"{len(tasks)} 个独立账户 Worker 并发运行中…",
        )

        try:
            self._active_worker_tasks = []
            for worker_no, task in enumerate(
                tasks,
                start=1,
            ):
                state = self.workers[worker_no]
                self._active_worker_tasks.append(
                    asyncio.create_task(
                        self._run_worker(
                            state,
                            task,
                            workspace=workspace,
                            batch_size=batch_size,
                            max_batches=max_batches,
                            export_fields=export_fields,
                        )
                    )
                )

            results = await asyncio.gather(
                *self._active_worker_tasks,
                return_exceptions=True,
            )

            failures = []
            total = 0
            batches = 0

            for i, result in enumerate(
                results,
                start=1,
            ):
                if isinstance(result, Exception):
                    failures.append(
                        f"W{i}: {result}"
                    )
                    self.emit(
                        "log",
                        f"[W{i}] 任务失败：{result}",
                    )
                else:
                    total += int(
                        result.get(
                            "result_count",
                            0,
                        )
                    )
                    batches += len(
                        result.get(
                            "exported_batches",
                            [],
                        )
                    )

            if failures:
                raise RuntimeError(
                    f"{len(failures)}/{len(tasks)} 个 Worker 失败："
                    + " | ".join(failures)
                )

            self.emit(
                "done",
                {
                    "result_count": total,
                    "batches": batches,
                    "shards": len(tasks),
                },
            )
            self.emit(
                "status",
                f"{len(tasks)} 个账户 Worker 均完成；"
                "各浏览器保持打开。",
            )
        except asyncio.CancelledError:
            self.emit(
                "log",
                "多账户任务已立即停止；"
                "已完成 XLS 保留，未完成批次下次重做。",
            )
            self.emit(
                "status",
                "任务已停止；各浏览器保持打开",
            )
        except Exception as exc:
            self.emit(
                "status",
                "部分/全部 Worker 中断；浏览器保持打开",
            )
            self.emit(
                "error",
                f"多账户任务中断：{exc}",
            )
        finally:
            self._active_worker_tasks = []
            self._active_controller_task = None
            self.running = False
            self.emit("running", False)

    async def stop_async(self):
        for state in self.workers.values():
            state.exporter.stop_requested = True

        self.emit(
            "status",
            "正在立即停止全部 Worker…",
        )

        current = asyncio.current_task()
        for task in list(
            self._active_worker_tasks
        ):
            if (
                task
                and not task.done()
                and task is not current
            ):
                task.cancel()

        controller = self._active_controller_task
        if (
            controller
            and not controller.done()
            and controller is not current
        ):
            controller.cancel()

    async def shutdown_async(self):
        await self._close_workers()
        self.emit("browser", False)


app.BrowserBackend = MultiAccountBackend


class ExtraWorkerWidget(QWidget):
    def __init__(
        self,
        index: int,
        data: dict[str, Any] | None = None,
    ):
        super().__init__()
        data = data or {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setRowWrapPolicy(
            QFormLayout.WrapLongRows
        )
        form.setFieldGrowthPolicy(
            QFormLayout.AllNonFixedFieldsGrow
        )

        self.enabled = QCheckBox(
            "启用这个 Worker"
        )
        self.enabled.setChecked(
            bool(data.get("enabled", True))
        )
        form.addRow("", self.enabled)

        self.account_note = QLineEdit(
            data.get(
                "account_note",
                f"账户 {index}",
            )
        )
        self.account_note.setFixedHeight(36)
        form.addRow(
            "账户备注",
            self.account_note,
        )

        self.collection = QLineEdit(
            data.get("id", f"Q{index}")
        )
        self.collection.setFixedHeight(36)
        form.addRow("集合 ID", self.collection)

        self.name = QLineEdit(
            data.get(
                "name",
                f"Query {index}",
            )
        )
        self.name.setFixedHeight(36)
        form.addRow("集合名称", self.name)

        years = QHBoxLayout()
        self.year_from = QSpinBox()
        self.year_from.setRange(0, 2100)
        self.year_from.setSpecialValueText(
            "自动"
        )
        self.year_from.setValue(
            int(data.get("year_from", 0))
        )
        self.year_from.setFixedHeight(36)

        self.year_to = QSpinBox()
        self.year_to.setRange(0, 2100)
        self.year_to.setSpecialValueText(
            "自动"
        )
        self.year_to.setValue(
            int(data.get("year_to", 0))
        )
        self.year_to.setFixedHeight(36)

        years.addWidget(self.year_from)
        years.addWidget(QLabel("至"))
        years.addWidget(self.year_to)
        form.addRow("分年范围", years)

        layout.addLayout(form)
        layout.addWidget(
            QLabel("专业检索式")
        )

        self.query = QPlainTextEdit()
        self.query.setFixedHeight(180)
        self.query.setPlainText(
            data.get("query", "")
        )
        layout.addWidget(self.query)

        profile_hint = QLabel(
            f"Worker {index} 使用独立 profile；"
            "请登录与其他 Worker 不同的 CNKI 账户。"
        )
        profile_hint.setObjectName("Hint")
        profile_hint.setWordWrap(True)
        layout.addWidget(profile_hint)

    def data(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled.isChecked(),
            "account_note": (
                self.account_note.text().strip()
            ),
            "id": self.collection.text().strip(),
            "name": self.name.text().strip(),
            "query": self.query.toPlainText().strip(),
            "year_from": self.year_from.value(),
            "year_to": self.year_to.value(),
        }


_original_window_init = app.MainWindow.__init__


def _worker_window_init(
    self,
    *args,
    **kwargs,
):
    _original_window_init(
        self,
        *args,
        **kwargs,
    )

    self.setWindowTitle(
        f"CNKI Metadata Exporter · "
        f"{app.APP_VERSION} · 多账户 Worker 版"
    )

    self.extra_worker_widgets: list[
        ExtraWorkerWidget
    ] = []

    query_card = self.query.parentWidget()
    qv = query_card.layout()

    self.query.setFixedHeight(190)

    # Worker 1 account note.
    worker1_row = QHBoxLayout()
    worker1_row.addWidget(
        QLabel("Worker 1 账户备注")
    )
    self.account1 = QLineEdit("账户 1")
    self.account1.setFixedHeight(36)
    worker1_row.addWidget(self.account1, 1)
    qv.insertLayout(1, worker1_row)

    bar = QHBoxLayout()
    title = QLabel("附加账户 Worker")
    title.setObjectName("FieldTitle")
    bar.addWidget(title)
    bar.addStretch(1)

    self.add_worker_btn = QPushButton(
        "＋ 添加 Worker"
    )
    self.add_worker_btn.setProperty(
        "kind",
        "secondary",
    )
    bar.addWidget(self.add_worker_btn)

    self.remove_worker_btn = QPushButton(
        "－ 删除当前"
    )
    self.remove_worker_btn.setProperty(
        "kind",
        "ghost",
    )
    bar.addWidget(self.remove_worker_btn)

    qv.addSpacing(10)
    qv.addLayout(bar)

    hint = QLabel(
        "每个启用 Worker = 一个独立浏览器 profile + 一个独立 CNKI 账户。"
        "不同 Worker 可以并发；同一 Worker 内的年份分片顺序执行。"
        "程序不会把后续 Query/导出页创建到 Worker 1 的浏览器里。"
    )
    hint.setObjectName("Hint")
    hint.setWordWrap(True)
    qv.addWidget(hint)

    self.worker_tabs = QTabWidget()
    self.worker_tabs.setDocumentMode(True)
    self.worker_tabs.setMinimumHeight(440)
    self.worker_tabs.setMaximumHeight(500)
    qv.addWidget(self.worker_tabs)

    self.add_worker_btn.clicked.connect(
        self.add_extra_worker
    )
    self.remove_worker_btn.clicked.connect(
        self.remove_extra_worker
    )

    saved = {}
    try:
        p = self.settings_path()
        if p.exists():
            saved = json.loads(
                p.read_text(encoding="utf-8")
            )
    except Exception:
        saved = {}

    self.account1.setText(
        saved.get(
            "worker1_account_note",
            "账户 1",
        )
    )

    extras = saved.get(
        "extra_workers",
        [],
    )
    if extras:
        for item in extras:
            self.add_extra_worker(item)
    else:
        self.add_extra_worker(
            {
                "enabled": False,
                "account_note": "账户 2",
                "id": "Q2",
                "name": "Query 2",
                "query": "",
                "year_from": 0,
                "year_to": 0,
            }
        )

    self.browser_btn.setText(
        "启动 / 重建 Worker 浏览器"
    )
    self.status.setText(
        "先启动各 Worker 浏览器并分别登录不同账户；"
        "再检查登录并运行。"
    )


app.MainWindow.__init__ = _worker_window_init


def _add_extra_worker(
    self,
    data=None,
):
    index = (
        len(self.extra_worker_widgets) + 2
    )
    if index > 4:
        QMessageBox.information(
            self,
            "Worker 上限",
            "当前版本最多 4 个账户 Worker。",
        )
        return

    widget = ExtraWorkerWidget(
        index,
        data,
    )
    self.extra_worker_widgets.append(widget)
    self.worker_tabs.addTab(
        widget,
        f"Worker {index}",
    )
    self.worker_tabs.setCurrentWidget(widget)


def _remove_extra_worker(self):
    idx = self.worker_tabs.currentIndex()
    if idx < 0:
        return

    widget = self.worker_tabs.widget(idx)
    self.worker_tabs.removeTab(idx)
    try:
        self.extra_worker_widgets.remove(widget)
    except ValueError:
        pass
    widget.deleteLater()

    for i in range(
        self.worker_tabs.count()
    ):
        self.worker_tabs.setTabText(
            i,
            f"Worker {i + 2}",
        )


app.MainWindow.add_extra_worker = (
    _add_extra_worker
)
app.MainWindow.remove_extra_worker = (
    _remove_extra_worker
)


_original_save_settings = (
    app.MainWindow.save_settings
)


def _save_worker_settings(self):
    _original_save_settings(self)
    try:
        p = self.settings_path()
        data = {}
        if p.exists():
            data = json.loads(
                p.read_text(encoding="utf-8")
            )

        data["worker1_account_note"] = (
            self.account1.text().strip()
        )
        data["extra_workers"] = [
            w.data()
            for w in self.extra_worker_widgets
        ]

        p.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


app.MainWindow.save_settings = (
    _save_worker_settings
)


def _collect_worker_tasks(
    self,
) -> list[dict[str, Any]]:
    tasks = []

    base_query = (
        self.query.toPlainText().strip()
    )
    base_id = self.collection.text().strip()
    base_name = (
        self.name.text().strip()
        or base_id
    )

    if not base_query:
        raise ValueError(
            "Worker 1 的专业检索式不能为空。"
        )

    y1 = self.year_from.value()
    y2 = self.year_to.value()

    tasks.append(
        {
            "account_note": (
                self.account1.text().strip()
                or "账户 1"
            ),
            "id": base_id,
            "name": base_name,
            "query": base_query,
            "year_from": (
                None if y1 == 0 else y1
            ),
            "year_to": (
                None if y2 == 0 else y2
            ),
        }
    )

    for widget in self.extra_worker_widgets:
        data = widget.data()
        if not data["enabled"]:
            continue
        if not data["query"]:
            raise ValueError(
                f"{data['id'] or '附加 Worker'} "
                "已启用，但 Query 为空。"
            )

        tasks.append(
            {
                "account_note": (
                    data["account_note"]
                    or f"账户 {len(tasks) + 1}"
                ),
                "id": data["id"],
                "name": (
                    data["name"]
                    or data["id"]
                ),
                "query": data["query"],
                "year_from": (
                    None
                    if data["year_from"] == 0
                    else data["year_from"]
                ),
                "year_to": (
                    None
                    if data["year_to"] == 0
                    else data["year_to"]
                ),
            }
        )

    if len(tasks) > 4:
        raise ValueError(
            "当前版本最多 4 个账户 Worker。"
        )

    ids = [task["id"] for task in tasks]
    if any(
        not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",
            cid or "",
        )
        for cid in ids
    ):
        raise ValueError(
            "每个集合 ID 只能使用英文字母、数字、"
            "点、下划线和连字符。"
        )

    if len(set(ids)) != len(ids):
        raise ValueError(
            "不同 Worker 的集合 ID 必须互不相同，"
            "否则输出目录会冲突。"
        )

    for task in tasks:
        a = task["year_from"]
        b = task["year_to"]
        if (a is None) ^ (b is None):
            raise ValueError(
                f"{task['id']} 的分年起始/结束"
                "要么都自动，要么都填写。"
            )
        if (
            a is not None
            and b is not None
            and a > b
        ):
            task["year_from"], task["year_to"] = (
                b,
                a,
            )

    return tasks


app.MainWindow.collect_worker_tasks = (
    _collect_worker_tasks
)


def _open_worker_browsers(self):
    try:
        wp = self.workspace_path()
        tasks = self.collect_worker_tasks()
    except Exception as exc:
        QMessageBox.warning(
            self,
            "配置错误",
            str(exc),
        )
        return

    self.save_settings()
    self.browser_btn.setEnabled(False)
    self.pill.set_state(
        "warn",
        "正在启动 Worker…",
    )
    self.backend.submit(
        self.backend.open_workers_async(
            wp,
            tasks,
        )
    )


app.MainWindow.open_browser = (
    _open_worker_browsers
)


def _start_worker_export(self):
    try:
        wp = self.workspace_path()
        tasks = self.collect_worker_tasks()
        fields = self.selected_fields()
        if not fields:
            raise ValueError(
                "至少选择一个希望导出的字段。"
            )
        if self.batch.value() % 50 != 0:
            raise ValueError(
                "每批条数必须是 50 的整数倍。"
            )
        max_batches = (
            None
            if self.max_batches.value() == 0
            else self.max_batches.value()
        )
    except Exception as exc:
        QMessageBox.warning(
            self,
            "配置错误",
            str(exc),
        )
        return

    self.save_settings()
    self.progress.setValue(0)
    self.progress_title.setText(
        f"准备运行 {len(tasks)} 个账户 Worker"
    )

    self.backend.submit(
        self.backend.run_workers_async(
            workspace=wp,
            tasks=tasks,
            batch_size=self.batch.value(),
            max_batches=max_batches,
            export_fields=fields,
        )
    )


app.MainWindow.start_export = (
    _start_worker_export
)


def main():
    app.main()


if __name__ == "__main__":
    main()
