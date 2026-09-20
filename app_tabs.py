from __future__ import annotations

import asyncio
import json
import math
import re
import traceback
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
app.APP_VERSION = "0.8.2-qt-tabs"


class TabbedExporter(app.RobustNativeExporter):
    """One logical CNKI export task bound to a page in a shared browser context."""

    def __init__(self, root: Path, profile_dir: Path, ui_queue, label: str):
        super().__init__(root, profile_dir, ui_queue)
        self.task_label = label

    def log(self, message: str) -> None:
        # Avoid calling RobustNativeExporter.log because that would add an
        # unprefixed UI message before we can tag it.
        try:
            super(app.RobustNativeExporter, self).log(message)
        except Exception:
            pass
        self.emit("log", f"[{self.task_label}] {message}")

    async def open_custom_export(self, page):
        """Capture the popup from the correct source tab.

        context.expect_page() is ambiguous when several tabs export at once.
        page.expect_popup() binds the export popup to the tab that clicked it.
        """
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
            # Fallback: find a new export tab, preferring the newest one.
            pages = [
                p
                for p in self.context.pages
                if "dm8/manage/export.html" in p.url
            ]
            if not pages:
                raise RuntimeError("自定义导出页面未打开")
            export_page = pages[-1]

        await export_page.wait_for_load_state("domcontentloaded", timeout=60000)
        await self.wait_for_human_verification(export_page)
        await export_page.locator("#litoexcel, .export-sidebar-a").first.wait_for(
            state="visible", timeout=30000
        )
        fields = export_page.locator(
            "input[name='SELFDEFINE_selfFiledList'], input[name='newdefine_selfFiledList']"
        )
        if await fields.count() < 4:
            side_link = export_page.locator("a[displaymode='selfDefine']").first
            if not await side_link.count():
                side_link = export_page.get_by_text("自定义", exact=True).last
            if await side_link.count():
                try:
                    await side_link.click(force=True)
                except Exception:
                    await side_link.evaluate("e => e.click()")
                await fields.first.wait_for(state="visible", timeout=30000)
        return export_page

    async def export_native_collection_on_page(
        self,
        page,
        collection: dict[str, Any],
        *,
        batch_size: int = 500,
        max_batches: int | None = None,
    ) -> dict[str, Any]:
        if self.context is None:
            raise RuntimeError("浏览器未启动")

        cid = str(collection["id"])
        native_dir = self.output_dir / "native_exports" / cid
        native_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = native_dir / "manifest.json"

        self.log(f"{cid} 开始知网原生元数据导出：{collection['name']}")
        total, pages, per_page = await self.search_collection(page, collection)
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
                "exported_batches": [],
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.log(f"{cid} 无命中记录")
            return manifest

        pages_per_batch = max(1, batch_size // per_page)
        batch_count = math.ceil(pages / pages_per_batch) if pages else 0
        if max_batches is not None:
            batch_count = min(batch_count, max_batches)
        exported: list[dict[str, Any]] = []

        for batch_index in range(batch_count):
            await self.check_stop()
            first_page = batch_index * pages_per_batch + 1
            last_page = min(pages, first_page + pages_per_batch - 1)
            first_record = (first_page - 1) * per_page + 1
            last_record = min(total, last_page * per_page)
            destination = (
                native_dir
                / f"{cid}_{first_record:05d}-{last_record:05d}.xls"
            )

            if destination.exists() and destination.stat().st_size > 0:
                self.log(
                    f"{cid} 已存在批次 {batch_index + 1}/{batch_count}："
                    f"{destination.name}"
                )
                exported.append(
                    {
                        "batch": batch_index + 1,
                        "first_record": first_record,
                        "last_record": last_record,
                        "expected_records": last_record - first_record + 1,
                        "file": destination.relative_to(self.root).as_posix(),
                        "bytes": destination.stat().st_size,
                        "status": "existing",
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
                        raise RuntimeError(f"{cid} 第 {pno} 页后无法翻页")

            selected = await self.selected_count(page)
            expected = last_record - first_record + 1
            if selected != expected or selected_expected != expected:
                raise RuntimeError(
                    f"{cid} 批次选择数量异常：页面显示 {selected}，"
                    f"累计新增 {selected_expected}，应为 {expected}。"
                    "如果多个标签页同时运行时出现此错误，说明知网把“已选文献”"
                    "状态跨标签页共享，需要进一步改为选择/导出临界区排队。"
                )

            last_export_error: Exception | None = None
            for export_attempt in range(1, 5):
                await self.check_stop()
                export_page = None
                try:
                    export_page = await self.open_custom_export(page)
                    await self.download_xls(export_page, destination)
                    last_export_error = None
                    break
                except Exception as exc:
                    last_export_error = exc
                    self.log(
                        f"{cid} 批次 {batch_index + 1} 第 {export_attempt} 次 "
                        f"XLS 导出失败：{exc}"
                    )
                    await page.wait_for_timeout(export_attempt * 3000)
                finally:
                    if export_page is not None and not export_page.is_closed():
                        try:
                            await export_page.close()
                        except Exception:
                            pass

            if last_export_error is not None:
                raise RuntimeError(
                    f"{cid} 批次 {batch_index + 1} 多次 XLS 导出失败"
                ) from last_export_error

            exported.append(
                {
                    "batch": batch_index + 1,
                    "first_record": first_record,
                    "last_record": last_record,
                    "expected_records": expected,
                    "file": destination.relative_to(self.root).as_posix(),
                    "bytes": destination.stat().st_size,
                    "status": "downloaded",
                }
            )
            self.log(
                f"{cid} 批次 {batch_index + 1}/{batch_count} 已下载："
                f"{destination.name}"
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
            "exported_batches": exported,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.log(f"{cid} 原生导出完成：{len(exported)} 个批次")
        return manifest


class MultiTabBackend(app.BrowserBackend):
    def __init__(self, ui_queue):
        super().__init__(ui_queue)
        self.task_exporters: list[TabbedExporter] = []
        self.task_pages = []
        self._task_handles: list[asyncio.Task] = []
        self._controller_task: asyncio.Task | None = None

    async def open_browser_async(self, workspace: Path) -> None:
        try:
            if self.browser_ready and self.exporter and self.workspace == workspace:
                self.emit("log", "浏览器已经在运行；多 Query 将在这个浏览器中打开不同标签页。")
                return

            if self.exporter:
                try:
                    await self.exporter.close()
                except Exception:
                    pass

            workspace.mkdir(parents=True, exist_ok=True)
            self.workspace = workspace
            self.exporter = app.RobustNativeExporter(
                workspace,
                workspace / ".cnki-tabs-profile",
                self.ui_queue,
            )
            self.emit("status", "正在启动单浏览器多标签页会话…")
            await self.exporter.launch()
            self.browser_ready = True
            self.emit("browser", True)
            self.emit(
                "status",
                "浏览器已启动；只需登录一次。运行后不同 Query 会打开不同标签页。",
            )
            self.emit(
                "log",
                f"共享登录 profile：{workspace / '.cnki-tabs-profile'}",
            )
        except Exception as exc:
            self.browser_ready = False
            self.emit("browser", False)
            self.emit("error", f"浏览器启动失败：{exc}\n\n{traceback.format_exc()}")

    def _task_exporter(self, label: str, export_fields: set[str]) -> TabbedExporter:
        exp = TabbedExporter(
            self.workspace,
            self.workspace / ".cnki-tabs-profile",
            self.ui_queue,
            label,
        )
        exp.context = self.exporter.context
        exp._playwright = self.exporter._playwright
        exp.export_fields = set(export_fields)
        exp.stop_requested = False
        return exp

    async def _prepare_pages(self, count: int):
        context = self.exporter.context
        pages = list(context.pages)

        # Keep one existing non-export page for Task 1, then create the rest.
        usable = [p for p in pages if "dm8/manage/export.html" not in p.url]
        if not usable:
            usable = [await context.new_page()]

        result = [usable[0]]
        while len(result) < count:
            result.append(await context.new_page())

        # Do not close unrelated user tabs. Only return pages assigned to tasks.
        return result

    async def _run_one_query(
        self,
        task_no: int,
        task: dict[str, Any],
        page,
        exporter: TabbedExporter,
        *,
        workspace: Path,
        batch_size: int,
        max_batches: int | None,
        export_fields: set[str],
    ):
        cid = task["id"]
        name = task["name"]
        query = task["query"]
        year_from = task.get("year_from")
        year_to = task.get("year_to")

        try:
            await page.bring_to_front()
        except Exception:
            pass

        exporter.log(f"标签页已分配：{cid} / {name}")
        await exporter.submit_professional_query(page, query)
        base_total = int(await exporter.result_count(page))
        exporter.log(f"主检索命中 {base_total} 条。")

        if base_total <= app.CNKI_RESULT_CAP:
            self.guard_resume_state(
                workspace, cid, query, name, export_fields
            )
            collection = {
                "id": cid,
                "name": name,
                "query": query,
                "field": query.split("=", 1)[0] if "=" in query else "",
                "purpose": "Qt shared-browser multi-tab",
                "tier": "GUI-MULTITAB",
            }
            return await exporter.export_native_collection_on_page(
                page,
                collection,
                batch_size=batch_size,
                max_batches=max_batches,
            )

        q_lo, q_hi = app.infer_year_range_from_query(query)
        lo = year_from or q_lo
        hi = year_to or q_hi
        if lo is None or hi is None:
            raise RuntimeError(
                f"{cid} 命中 {base_total} 条，超过知网 6000 条上限；"
                "请为这个 Query 填写分年起始/结束。"
            )
        if lo > hi:
            lo, hi = hi, lo

        exporter.log(
            f"超过 6000 条，按年份 {lo}–{hi} 在本标签页内顺序分片。"
        )

        remaining = max_batches
        manifests = []
        for year in range(lo, hi + 1):
            await exporter.check_stop()
            shard_query = app.year_shard_query(query, year)
            shard_id = f"{cid}__Y{year}"
            await exporter.submit_professional_query(page, shard_query)
            count = int(await exporter.result_count(page))
            exporter.log(f"分片预检：{year} 年 = {count} 条。")
            if count > app.CNKI_RESULT_CAP:
                raise RuntimeError(
                    f"{cid} / {year} 年仍有 {count} 条，单年也超过 6000；"
                    "需要第二维度拆分。"
                )
            if count <= 0:
                continue

            before = len(self.shard_batch_files(workspace, shard_id))
            if remaining is not None and remaining <= 0:
                break

            shard_limit = None if remaining is None else before + remaining
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
                "field": query.split("=", 1)[0] if "=" in query else "",
                "purpose": f"Qt shared-browser multi-tab {year}",
                "tier": "GUI-MULTITAB-YEAR",
            }
            manifest = await exporter.export_native_collection_on_page(
                page,
                collection,
                batch_size=batch_size,
                max_batches=shard_limit,
            )
            manifests.append(manifest)

            if remaining is not None:
                after = len(self.shard_batch_files(workspace, shard_id))
                remaining -= max(0, after - before)

        return {
            "collection": cid,
            "name": name,
            "query": query,
            "result_count": base_total,
            "exported_batches": [
                b
                for manifest in manifests
                for b in manifest.get("exported_batches", [])
            ],
        }

    async def run_queries_async(
        self,
        *,
        workspace: Path,
        tasks: list[dict[str, Any]],
        batch_size: int,
        max_batches: int | None,
        export_fields: set[str],
    ) -> None:
        if self.running:
            self.emit("error", "已有多 Query 任务正在运行。")
            return
        if not self.exporter or not self.exporter.context:
            self.emit("error", "请先打开浏览器并登录。")
            return

        self.running = True
        self._controller_task = asyncio.current_task()
        self.emit("running", True)
        self.emit(
            "status",
            f"正在为 {len(tasks)} 个 Query 准备 {len(tasks)} 个浏览器标签页…",
        )

        try:
            pages = await self._prepare_pages(len(tasks))
            self.task_pages = pages
            self.task_exporters = [
                self._task_exporter(f"T{i}", export_fields)
                for i in range(1, len(tasks) + 1)
            ]

            self.emit(
                "log",
                f"多 Query 模式：1 个浏览器、1 次登录、{len(tasks)} 个独立标签页并发。",
            )
            self.emit(
                "log",
                "每个标签页绑定一个 Query；导出弹窗使用来源标签页自己的 popup 监听，"
                "避免多个 Query 的导出窗口串线。",
            )

            self._task_handles = [
                asyncio.create_task(
                    self._run_one_query(
                        i,
                        task,
                        page,
                        exporter,
                        workspace=workspace,
                        batch_size=batch_size,
                        max_batches=max_batches,
                        export_fields=export_fields,
                    )
                )
                for i, (task, page, exporter) in enumerate(
                    zip(tasks, pages, self.task_exporters),
                    start=1,
                )
            ]

            results = await asyncio.gather(
                *self._task_handles,
                return_exceptions=True,
            )

            failures = []
            total = 0
            batches = 0
            for i, result in enumerate(results, start=1):
                if isinstance(result, Exception):
                    failures.append(f"T{i}: {result}")
                    self.emit("log", f"[T{i}] 任务失败：{result}")
                else:
                    total += int(result.get("result_count", 0))
                    batches += len(result.get("exported_batches", []))

            if failures:
                raise RuntimeError(
                    f"{len(failures)}/{len(tasks)} 个 Query 失败："
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
                f"{len(tasks)} 个 Query 均完成；浏览器及结果标签页保持打开",
            )
        except asyncio.CancelledError:
            self.emit(
                "log",
                "多 Query 任务已停止；已完成 XLS 保留，未完成批次下次重做。",
            )
            self.emit("status", "任务已停止；浏览器保持打开")
        except Exception as exc:
            self.emit("status", "部分/全部 Query 中断；浏览器保持打开")
            self.emit("error", f"多 Query 任务中断：{exc}")
        finally:
            self._task_handles = []
            self._controller_task = None
            self.running = False
            self.emit("running", False)

    async def stop_async(self):
        if self.exporter:
            self.exporter.stop_requested = True
        for exp in self.task_exporters:
            exp.stop_requested = True

        self.emit("status", "正在立即停止全部 Query…")
        current = asyncio.current_task()
        for task in list(self._task_handles):
            if task and not task.done() and task is not current:
                task.cancel()
        controller = self._controller_task
        if controller and not controller.done() and controller is not current:
            controller.cancel()


app.BrowserBackend = MultiTabBackend


class ExtraTaskWidget(QWidget):
    def __init__(self, index: int, data: dict[str, Any] | None = None):
        super().__init__()
        data = data or {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self.enabled = QCheckBox("启用这个 Query")
        self.enabled.setChecked(bool(data.get("enabled", True)))
        form.addRow("", self.enabled)

        self.collection = QLineEdit(data.get("id", f"Q{index}"))
        self.collection.setFixedHeight(36)
        form.addRow("集合 ID", self.collection)

        self.name = QLineEdit(data.get("name", f"Query {index}"))
        self.name.setFixedHeight(36)
        form.addRow("集合名称", self.name)

        years = QHBoxLayout()
        self.year_from = QSpinBox()
        self.year_from.setRange(0, 2100)
        self.year_from.setSpecialValueText("自动")
        self.year_from.setValue(int(data.get("year_from", 0)))
        self.year_from.setFixedHeight(36)
        self.year_to = QSpinBox()
        self.year_to.setRange(0, 2100)
        self.year_to.setSpecialValueText("自动")
        self.year_to.setValue(int(data.get("year_to", 0)))
        self.year_to.setFixedHeight(36)
        years.addWidget(self.year_from)
        years.addWidget(QLabel("至"))
        years.addWidget(self.year_to)
        form.addRow("分年范围", years)

        layout.addLayout(form)
        layout.addWidget(QLabel("专业检索式"))
        self.query = QPlainTextEdit()
        self.query.setFixedHeight(180)
        self.query.setPlainText(data.get("query", ""))
        layout.addWidget(self.query)

    def data(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled.isChecked(),
            "id": self.collection.text().strip(),
            "name": self.name.text().strip(),
            "query": self.query.toPlainText().strip(),
            "year_from": self.year_from.value(),
            "year_to": self.year_to.value(),
        }


_original_window_init = app.MainWindow.__init__


def _multiquery_window_init(self, *args, **kwargs):
    _original_window_init(self, *args, **kwargs)
    self.setWindowTitle(
        f"CNKI Metadata Exporter · {app.APP_VERSION} · 多 Query 标签页版"
    )

    self.extra_task_widgets: list[ExtraTaskWidget] = []

    query_card = self.query.parentWidget()
    qv = query_card.layout()

    # No nested splitter: the entire application is already inside the
    # scroll area from app_responsive. Give controls readable heights and let
    # the page scroll vertically instead of squeezing child containers.
    self.query.setFixedHeight(190)
    for control in (
        self.collection,
        self.name,
        self.batch,
        self.max_batches,
        self.year_from,
        self.year_to,
    ):
        control.setFixedHeight(36)

    bar = QHBoxLayout()
    title = QLabel("附加 Query 实例")
    title.setObjectName("FieldTitle")
    bar.addWidget(title)
    bar.addStretch(1)

    self.add_query_btn = QPushButton("＋ 添加 Query")
    self.add_query_btn.setProperty("kind", "secondary")
    bar.addWidget(self.add_query_btn)

    self.remove_query_btn = QPushButton("－ 删除当前")
    self.remove_query_btn.setProperty("kind", "ghost")
    bar.addWidget(self.remove_query_btn)

    qv.addSpacing(10)
    qv.addLayout(bar)

    hint = QLabel(
        "任务 1 使用上面的集合 ID / 名称 / Query；这里可继续添加任务 2–4。"
        "运行时只启动一个 Edge/Chrome，每个 Query 占一个浏览器标签页，共享一次登录。"
        "输入区域使用固定的可读高度；需要查看更多内容时直接滚动整个页面。"
    )
    hint.setObjectName("Hint")
    hint.setWordWrap(True)
    qv.addWidget(hint)

    self.query_tabs = QTabWidget()
    self.query_tabs.setDocumentMode(True)
    self.query_tabs.setMinimumHeight(420)
    self.query_tabs.setMaximumHeight(470)
    qv.addWidget(self.query_tabs)

    self.add_query_btn.clicked.connect(self.add_extra_query)
    self.remove_query_btn.clicked.connect(self.remove_extra_query)

    saved = {}
    try:
        p = self.settings_path()
        if p.exists():
            saved = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        saved = {}

    extras = saved.get("extra_queries", [])
    if extras:
        for item in extras:
            self.add_extra_query(item)
    else:
        self.add_extra_query(
            {
                "enabled": False,
                "id": "Q2",
                "name": "Query 2",
                "query": "",
                "year_from": 0,
                "year_to": 0,
            }
        )

    self.browser_btn.setText("打开 / 复用单浏览器")
    self.status.setText(
        "打开浏览器并登录一次；开始后不同 Query 会在同一浏览器中打开不同标签页。"
    )




app.MainWindow.__init__ = _multiquery_window_init


def _add_extra_query(self, data=None):
    index = len(self.extra_task_widgets) + 2
    widget = ExtraTaskWidget(index, data)
    self.extra_task_widgets.append(widget)
    self.query_tabs.addTab(widget, f"任务 {index}")
    self.query_tabs.setCurrentWidget(widget)


def _remove_extra_query(self):
    idx = self.query_tabs.currentIndex()
    if idx < 0:
        return
    widget = self.query_tabs.widget(idx)
    self.query_tabs.removeTab(idx)
    try:
        self.extra_task_widgets.remove(widget)
    except ValueError:
        pass
    widget.deleteLater()
    for i in range(self.query_tabs.count()):
        self.query_tabs.setTabText(i, f"任务 {i + 2}")


app.MainWindow.add_extra_query = _add_extra_query
app.MainWindow.remove_extra_query = _remove_extra_query


_original_save_settings = app.MainWindow.save_settings


def _save_multiquery_settings(self):
    _original_save_settings(self)
    try:
        p = self.settings_path()
        data = {}
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
        data["extra_queries"] = [w.data() for w in self.extra_task_widgets]
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


app.MainWindow.save_settings = _save_multiquery_settings


def _collect_tasks(self) -> list[dict[str, Any]]:
    tasks = []

    base_query = self.query.toPlainText().strip()
    base_id = self.collection.text().strip()
    base_name = self.name.text().strip() or base_id
    base_y1 = self.year_from.value()
    base_y2 = self.year_to.value()

    if base_query:
        tasks.append(
            {
                "id": base_id,
                "name": base_name,
                "query": base_query,
                "year_from": None if base_y1 == 0 else base_y1,
                "year_to": None if base_y2 == 0 else base_y2,
            }
        )

    for widget in self.extra_task_widgets:
        data = widget.data()
        if not data["enabled"]:
            continue
        if not data["query"]:
            raise ValueError(f"{data['id'] or '附加任务'} 已启用，但 Query 为空。")
        tasks.append(
            {
                "id": data["id"],
                "name": data["name"] or data["id"],
                "query": data["query"],
                "year_from": None
                if data["year_from"] == 0
                else data["year_from"],
                "year_to": None if data["year_to"] == 0 else data["year_to"],
            }
        )

    if not tasks:
        raise ValueError("至少需要一个 Query。")
    if len(tasks) > 4:
        raise ValueError("当前版本最多同时运行 4 个 Query 标签页。")

    ids = [x["id"] for x in tasks]
    if any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", cid or "")
        for cid in ids
    ):
        raise ValueError("每个集合 ID 都只能使用英文字母、数字、点、下划线和连字符。")
    if len(set(ids)) != len(ids):
        raise ValueError("多个 Query 的集合 ID 必须互不相同，否则输出目录会冲突。")

    for task in tasks:
        a, b = task["year_from"], task["year_to"]
        if (a is None) ^ (b is None):
            raise ValueError(
                f"{task['id']} 的分年起始/结束要么都自动，要么都填写。"
            )
        if a is not None and b is not None and a > b:
            task["year_from"], task["year_to"] = b, a

    return tasks


app.MainWindow.collect_tasks = _collect_tasks


def _start_multiquery_export(self):
    try:
        wp = self.workspace_path()
        tasks = self.collect_tasks()
        fields = self.selected_fields()
        if not fields:
            raise ValueError("至少选择一个希望导出的字段。")
        if self.batch.value() % 50 != 0:
            raise ValueError("每批条数必须是 50 的整数倍。")
        max_batches = (
            None if self.max_batches.value() == 0 else self.max_batches.value()
        )
    except Exception as exc:
        QMessageBox.warning(self, "配置错误", str(exc))
        return

    self.save_settings()
    self.progress.setValue(0)
    self.progress_title.setText(f"准备运行 {len(tasks)} 个 Query")
    self.backend.submit(
        self.backend.run_queries_async(
            workspace=wp,
            tasks=tasks,
            batch_size=self.batch.value(),
            max_batches=max_batches,
            export_fields=fields,
        )
    )


app.MainWindow.start_export = _start_multiquery_export


def main():
    app.main()


if __name__ == "__main__":
    main()
