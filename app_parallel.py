from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

import app_responsive

app = app_responsive.app
app.APP_VERSION = "0.7.1-qt"


class PrefixedQueue:
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


class ParallelBrowserBackend(app.BrowserBackend):
    def __init__(self, ui_queue):
        super().__init__(ui_queue)
        self.exporters: list[app.RobustNativeExporter] = []
        self.concurrency = 1
        self._active_controller_task: asyncio.Task | None = None
        self._active_worker_tasks: list[asyncio.Task] = []

    @property
    def exporter(self):
        return self.exporters[0] if self.exporters else None

    @exporter.setter
    def exporter(self, value):
        # The parent initializer assigns self.exporter = None.
        if value is None:
            if hasattr(self, "exporters"):
                self.exporters.clear()
            return
        if not hasattr(self, "exporters"):
            self.exporters = []
        if self.exporters:
            self.exporters[0] = value
        else:
            self.exporters.append(value)

    async def _launch_one(self, workspace: Path, worker_no: int):
        # Never reuse the single-instance profile in concurrent mode.
        # A Chromium-family browser places a process singleton lock inside its
        # user-data-dir. Reusing ".cnki-profile" while an older single-instance
        # browser is still open makes Edge/Chrome exit immediately with
        # "opened in an existing browser session".
        profile = workspace / ".cnki-parallel" / f"W{worker_no}"
        exporter = app.RobustNativeExporter(
            workspace,
            profile,
            PrefixedQueue(self.ui_queue, worker_no),
        )
        await exporter.launch()
        return exporter

    async def open_browser_async(self, workspace: Path, concurrency: int = 1) -> None:
        concurrency = max(1, min(4, int(concurrency)))
        try:
            if (
                self.browser_ready
                and self.workspace == workspace
                and len(self.exporters) == concurrency
            ):
                self.emit(
                    "log",
                    f"{concurrency} 个并发浏览器已经运行，继续复用各自的登录会话。",
                )
                return

            await self._close_exporters()

            workspace.mkdir(parents=True, exist_ok=True)
            self.workspace = workspace
            self.concurrency = concurrency
            self.emit("status", f"正在启动 {concurrency} 个独立浏览器…")

            # Start worker browsers one by one. They run concurrently after
            # startup, but serial startup avoids Chromium/Edge process-singleton
            # races seen when several persistent contexts are launched at once.
            exporters: list[app.RobustNativeExporter] = []
            for i in range(1, concurrency + 1):
                try:
                    self.emit("status", f"正在启动并发浏览器 W{i}/{concurrency}…")
                    exp = await self._launch_one(workspace, i)
                    exporters.append(exp)
                    await asyncio.sleep(0.8)
                except Exception as exc:
                    for opened in exporters:
                        try:
                            await opened.close()
                        except Exception:
                            pass
                    self.exporters = []
                    raise RuntimeError(f"W{i} 启动失败：{exc}") from exc

            self.exporters = exporters
            self.browser_ready = True
            self.emit("browser", True)
            self.emit(
                "status",
                f"已启动 {concurrency} 个独立浏览器；请分别完成 CNKI / CARSI 登录",
            )
            self.emit(
                "log",
                "并发模式使用相互隔离的浏览器 profile，避免不同任务的知网选择状态互相污染。",
            )
            for i in range(1, concurrency + 1):
                profile = workspace / ".cnki-parallel" / f"W{i}"
                self.emit("log", f"W{i} profile：{profile}")
        except Exception as exc:
            self.browser_ready = False
            self.emit("browser", False)
            self.emit("error", f"并发浏览器启动失败：{exc}")

    async def _login_state_one(self, exporter, worker_no: int):
        context = exporter.context
        if not context:
            return worker_no, False, "浏览器未就绪"
        page = context.pages[0] if context.pages else await context.new_page()
        if "cnki.net" not in page.url:
            await page.goto(
                "https://kns.cnki.net/kns8s/AdvSearch",
                wait_until="domcontentloaded",
                timeout=60000,
            )
        await exporter.wait_for_human_verification(page)

        unit = page.locator("div.ecp_header_unitName").first
        unit_text = ""
        if await unit.count():
            try:
                unit_text = (await unit.inner_text()).strip()
            except Exception:
                pass
        if unit_text:
            return worker_no, True, unit_text
        try:
            body = await page.locator("body").inner_text(timeout=5000)
        except Exception:
            body = ""
        if any(x in body for x in ("退出", "个人中心", "我的知网")):
            return worker_no, True, "已检测到账户登录"
        return worker_no, False, "未检测到明确机构/账户标记"

    async def check_login_async(self) -> None:
        if not self.exporters:
            self.emit("error", "请先启动并发浏览器。")
            return
        try:
            states = await asyncio.gather(
                *[
                    self._login_state_one(exp, i)
                    for i, exp in enumerate(self.exporters, start=1)
                ],
                return_exceptions=True,
            )
            ok = 0
            details = []
            for i, state in enumerate(states, start=1):
                if isinstance(state, Exception):
                    details.append(f"W{i}=检查失败")
                    continue
                worker_no, success, text = state
                ok += int(success)
                details.append(f"W{worker_no}={'✓' if success else '?'} {text}")
            level = "ok" if ok == len(self.exporters) else "warn"
            self.emit("login", (level, f"{ok}/{len(self.exporters)} 已确认登录"))
            self.emit("status", "；".join(details))
            self.emit("log", "并发登录检查：" + " | ".join(details))
        except Exception as exc:
            self.emit("error", f"检查并发登录状态失败：{exc}")

    async def query_total_on(self, exporter, query: str) -> int:
        if not exporter.context:
            raise RuntimeError("浏览器尚未启动。")
        page = (
            exporter.context.pages[0]
            if exporter.context.pages
            else await exporter.context.new_page()
        )
        await exporter.submit_professional_query(page, query)
        return int(await exporter.result_count(page))

    async def _precheck_shards(
        self,
        query: str,
        collection_id: str,
        lo: int,
        hi: int,
    ) -> tuple[list[dict[str, Any]], int]:
        # Precheck sequentially in W1 to avoid a burst of simultaneous search
        # requests before actual export begins.
        planner = self.exporters[0]
        shard_plan: list[dict[str, Any]] = []
        shard_sum = 0
        for year in range(lo, hi + 1):
            await planner.check_stop()
            shard_query = app.year_shard_query(query, year)
            count = await self.query_total_on(planner, shard_query)
            shard_id = f"{collection_id}__Y{year}"
            shard_plan.append(
                {
                    "year": year,
                    "collection_id": shard_id,
                    "query": shard_query,
                    "result_count": count,
                }
            )
            shard_sum += count
            self.emit("log", f"分片预检：{year} 年 = {count} 条。")
            if count > app.CNKI_RESULT_CAP:
                raise RuntimeError(
                    f"{year} 年单年仍命中 {count} 条，超过知网 "
                    f"{app.CNKI_RESULT_CAP} 条上限；该年份还需要第二维度拆分。"
                )
        return shard_plan, shard_sum

    async def _export_full_shard(
        self,
        exporter,
        worker_no: int,
        workspace: Path,
        name: str,
        query: str,
        export_fields: set[str],
        batch_size: int,
        shard: dict[str, Any],
    ) -> int:
        count = int(shard["result_count"])
        if count <= 0:
            return 0

        shard_id = str(shard["collection_id"])
        shard_query = str(shard["query"])
        year = int(shard["year"])
        self.guard_resume_state(
            workspace,
            shard_id,
            shard_query,
            f"{name} · {year}",
            export_fields,
        )
        exporter.export_fields = set(export_fields)
        exporter.stop_requested = False
        self.emit(
            "log",
            f"[W{worker_no}] 接管 {year} 年分片：{count} 条。",
        )
        collection = {
            "id": shard_id,
            "name": f"{name} · {year}",
            "query": shard_query,
            "field": query.split("=", 1)[0] if "=" in query else "",
            "purpose": f"Qt parallel year shard {year}",
            "tier": "GUI-PARALLEL",
        }
        manifest = await exporter.export_native_collection(
            collection,
            batch_size=batch_size,
            max_batches=None,
        )
        return len(manifest.get("exported_batches", []))

    async def _parallel_unlimited(
        self,
        *,
        workspace: Path,
        name: str,
        query: str,
        export_fields: set[str],
        batch_size: int,
        shard_plan: list[dict[str, Any]],
    ) -> tuple[int, int]:
        queue: asyncio.Queue = asyncio.Queue()
        for shard in shard_plan:
            if int(shard["result_count"]) > 0:
                queue.put_nowait(shard)

        totals = {"shards": 0, "batch_entries": 0}
        lock = asyncio.Lock()

        async def worker(worker_no: int, exporter):
            while True:
                await exporter.check_stop()
                try:
                    shard = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    batches = await self._export_full_shard(
                        exporter,
                        worker_no,
                        workspace,
                        name,
                        query,
                        export_fields,
                        batch_size,
                        shard,
                    )
                    async with lock:
                        totals["shards"] += 1
                        totals["batch_entries"] += batches
                finally:
                    queue.task_done()

        self._active_worker_tasks = [
            asyncio.create_task(worker(i, exp))
            for i, exp in enumerate(self.exporters, start=1)
        ]
        await asyncio.gather(*self._active_worker_tasks)
        self._active_worker_tasks = []
        return totals["shards"], totals["batch_entries"]

    async def _parallel_limited(
        self,
        *,
        workspace: Path,
        name: str,
        query: str,
        export_fields: set[str],
        batch_size: int,
        shard_plan: list[dict[str, Any]],
        max_batches: int,
    ) -> tuple[int, int]:
        # To preserve the old meaning of "最多批次", reserve one global batch
        # token at a time. This is intended mainly for test runs. Unlimited mode
        # is much faster because each worker can finish its current year in one go.
        queue: asyncio.Queue = asyncio.Queue()
        for shard in shard_plan:
            if int(shard["result_count"]) > 0:
                queue.put_nowait(shard)

        state = {"remaining": max_batches, "shards": set(), "new_batches": 0}
        lock = asyncio.Lock()

        async def worker(worker_no: int, exporter):
            while True:
                await exporter.check_stop()
                async with lock:
                    if state["remaining"] <= 0:
                        return
                try:
                    shard = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                try:
                    count = int(shard["result_count"])
                    shard_id = str(shard["collection_id"])
                    shard_query = str(shard["query"])
                    year = int(shard["year"])
                    expected_batches = math.ceil(count / batch_size)
                    before = len(self.shard_batch_files(workspace, shard_id))
                    if before >= expected_batches:
                        state["shards"].add(year)
                        continue

                    async with lock:
                        if state["remaining"] <= 0:
                            queue.put_nowait(shard)
                            return
                        state["remaining"] -= 1

                    self.guard_resume_state(
                        workspace,
                        shard_id,
                        shard_query,
                        f"{name} · {year}",
                        export_fields,
                    )
                    exporter.export_fields = set(export_fields)
                    exporter.stop_requested = False
                    self.emit(
                        "log",
                        f"[W{worker_no}] 测试配额：{year} 年导出下一个 500 条批次。",
                    )
                    collection = {
                        "id": shard_id,
                        "name": f"{name} · {year}",
                        "query": shard_query,
                        "field": query.split("=", 1)[0] if "=" in query else "",
                        "purpose": f"Qt parallel limited year shard {year}",
                        "tier": "GUI-PARALLEL",
                    }
                    await exporter.export_native_collection(
                        collection,
                        batch_size=batch_size,
                        max_batches=before + 1,
                    )
                    after = len(self.shard_batch_files(workspace, shard_id))
                    if after > before:
                        async with lock:
                            state["new_batches"] += after - before
                    if after >= expected_batches:
                        state["shards"].add(year)
                    else:
                        queue.put_nowait(shard)
                finally:
                    queue.task_done()

        self._active_worker_tasks = [
            asyncio.create_task(worker(i, exp))
            for i, exp in enumerate(self.exporters, start=1)
        ]
        await asyncio.gather(*self._active_worker_tasks)
        self._active_worker_tasks = []
        return len(state["shards"]), state["new_batches"]

    async def run_export_async(
        self,
        *,
        workspace: Path,
        collection_id: str,
        name: str,
        query: str,
        batch_size: int,
        max_batches: int | None,
        export_fields: set[str],
        year_from: int | None,
        year_to: int | None,
    ) -> None:
        if self.running:
            self.emit("error", "已有任务正在运行。")
            return
        if not self.exporters:
            self.emit("error", "请先启动并发浏览器并登录。")
            return

        self.running = True
        self._active_controller_task = asyncio.current_task()
        for exp in self.exporters:
            exp.stop_requested = False
            exp.export_fields = set(export_fields)
        self.emit("running", True)
        self.emit("status", "正在预检索…")

        try:
            planner = self.exporters[0]
            base_total = await self.query_total_on(planner, query)
            self.emit("log", f"主检索命中 {base_total} 条。")

            q_lo, q_hi = app.infer_year_range_from_query(query)
            lo = year_from or q_lo
            hi = year_to or q_hi

            need_shards = (
                base_total > app.CNKI_RESULT_CAP
                or (len(self.exporters) > 1 and lo is not None and hi is not None)
            )

            if not need_shards:
                self.emit(
                    "log",
                    "当前任务无需分片；并发浏览器已就绪，但此检索将只由 W1 执行。",
                )
                self.guard_resume_state(
                    workspace, collection_id, query, name, export_fields
                )
                collection = {
                    "id": collection_id,
                    "name": name,
                    "query": query,
                    "field": query.split("=", 1)[0] if "=" in query else "",
                    "purpose": "Qt GUI",
                    "tier": "GUI",
                }
                manifest = await planner.export_native_collection(
                    collection,
                    batch_size=batch_size,
                    max_batches=max_batches,
                )
                self.emit(
                    "done",
                    {
                        "result_count": manifest.get("result_count", 0),
                        "batches": len(manifest.get("exported_batches", [])),
                        "shards": 1,
                    },
                )
                self.emit("status", "任务完成；浏览器保持打开")
                return

            if lo is None or hi is None:
                raise RuntimeError(
                    f"当前检索命中 {base_total} 条，需要分片才能并发。"
                    "请填写“分年起始/结束”，或在检索式中提供可识别的 YE 年份条件。"
                )
            if lo > hi:
                lo, hi = hi, lo
            if lo < 1900 or hi > 2100:
                raise RuntimeError(f"年份范围不合理：{lo}–{hi}")

            self.emit(
                "log",
                f"并发分年模式：{lo}–{hi}，最多 {len(self.exporters)} 个独立浏览器同时工作。",
            )
            self.emit("status", f"正在规划并发分片：{lo}–{hi}")
            shard_plan, shard_sum = await self._precheck_shards(
                query, collection_id, lo, hi
            )
            self.write_shard_plan(
                workspace,
                collection_id,
                name,
                query,
                export_fields,
                lo,
                hi,
                shard_plan,
                base_total,
            )
            if shard_sum != base_total:
                self.emit(
                    "log",
                    f"注意：分年合计 {shard_sum} 条，主检索 {base_total} 条，"
                    f"相差 {shard_sum - base_total:+d}。",
                )

            if max_batches is None:
                completed_shards, batch_entries = await self._parallel_unlimited(
                    workspace=workspace,
                    name=name,
                    query=query,
                    export_fields=export_fields,
                    batch_size=batch_size,
                    shard_plan=shard_plan,
                )
            else:
                completed_shards, batch_entries = await self._parallel_limited(
                    workspace=workspace,
                    name=name,
                    query=query,
                    export_fields=export_fields,
                    batch_size=batch_size,
                    shard_plan=shard_plan,
                    max_batches=max_batches,
                )

            self.emit(
                "done",
                {
                    "result_count": shard_sum,
                    "batches": batch_entries,
                    "shards": completed_shards,
                },
            )
            self.emit(
                "status",
                "并发任务完成/已到本次批次限制；浏览器保持打开",
            )
        except asyncio.CancelledError:
            self.emit(
                "log",
                "并发任务已立即停止；已完成 XLS 批次保留，未完成批次下次重做。",
            )
            self.emit("status", "任务已停止；浏览器保持打开")
        except Exception as exc:
            self.emit("status", "任务中断；浏览器保持打开")
            self.emit("error", f"并发任务中断：{exc}")
        finally:
            self._active_worker_tasks = []
            self._active_controller_task = None
            self.running = False
            self.emit("running", False)

    async def stop_async(self):
        for exp in self.exporters:
            exp.stop_requested = True

        self.emit("status", "正在立即停止全部并发任务…")
        self.emit(
            "log",
            "已向全部 worker 发送停止请求；正在取消仍处于 await 的页面操作。",
        )

        current = asyncio.current_task()
        for task in list(self._active_worker_tasks):
            if task and not task.done() and task is not current:
                task.cancel()

        controller = self._active_controller_task
        if controller and not controller.done() and controller is not current:
            controller.cancel()

    async def _close_exporters(self):
        exporters = list(self.exporters)
        self.exporters = []
        for exp in exporters:
            exp.stop_requested = True
        await asyncio.gather(
            *[exp.close() for exp in exporters],
            return_exceptions=True,
        )
        self.browser_ready = False

    async def shutdown_async(self):
        await self._close_exporters()
        self.emit("browser", False)


app.BrowserBackend = ParallelBrowserBackend


_original_window_init = app.MainWindow.__init__


def _parallel_window_init(self, *args, **kwargs):
    _original_window_init(self, *args, **kwargs)

    self.setWindowTitle(f"CNKI Metadata Exporter · {app.APP_VERSION} · 并发版")

    self.concurrency = app.QSpinBox()
    self.concurrency.setRange(1, 4)
    self.concurrency.setValue(2)
    self.concurrency.setToolTip(
        "每个 worker 使用独立浏览器 profile。建议从 2 开始；"
        "并发越高越容易触发知网安全验证。"
    )
    self.concurrency.setPrefix("并发 ")

    session = self.browser_btn.parentWidget()
    grid = session.layout()
    grid.addWidget(app.QLabel("并发浏览器"), 2, 0)
    grid.addWidget(self.concurrency, 2, 3)

    self.browser_btn.setText("启动 / 复用并发浏览器")

    self.concurrent_hint = app.QLabel(
        "并发以“年份分片”为单位：不同 worker 不共享知网选择状态。"
        "建议并发 2；3–4 会增加验证/限流概率。"
    )
    self.concurrent_hint.setObjectName("Hint")
    self.concurrent_hint.setWordWrap(True)
    grid.addWidget(self.concurrent_hint, 3, 1, 1, 3)

    try:
        settings = {}
        p = self.settings_path()
        if p.exists():
            settings = app.json.loads(p.read_text(encoding="utf-8"))
        self.concurrency.setValue(int(settings.get("concurrency", 2)))
    except Exception:
        pass


app.MainWindow.__init__ = _parallel_window_init


def _parallel_open_browser(self):
    try:
        wp = self.workspace_path()
    except Exception as exc:
        app.QMessageBox.warning(self, "配置错误", str(exc))
        return
    self.save_settings()
    self.browser_btn.setEnabled(False)
    self.pill.set_state("warn", "正在启动…")
    self.backend.submit(
        self.backend.open_browser_async(wp, self.concurrency.value())
    )


app.MainWindow.open_browser = _parallel_open_browser


_original_save_settings = app.MainWindow.save_settings


def _parallel_save_settings(self):
    _original_save_settings(self)
    try:
        p = self.settings_path()
        data = {}
        if p.exists():
            data = app.json.loads(p.read_text(encoding="utf-8"))
        data["concurrency"] = int(self.concurrency.value())
        p.write_text(
            app.json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


app.MainWindow.save_settings = _parallel_save_settings


def main():
    app.main()


if __name__ == "__main__":
    main()
