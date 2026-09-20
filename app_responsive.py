from __future__ import annotations

import asyncio
from pathlib import Path

import app

app.APP_VERSION = "0.6.0-qt"


# ---------- Fast cancellation ----------
_original_run_export_async = app.BrowserBackend.run_export_async


async def _run_export_with_task_handle(self, *args, **kwargs):
    self._active_export_task = asyncio.current_task()
    try:
        return await _original_run_export_async(self, *args, **kwargs)
    finally:
        self._active_export_task = None


async def _fast_stop_async(self):
    if self.exporter:
        self.exporter.stop_requested = True

    self.emit("status", "正在立即停止…")
    self.emit(
        "log",
        "已发送立即停止请求；当前尚未写完的批次不会被记为完成，"
        "已成功下载的 XLS 批次保持不变。",
    )

    task = getattr(self, "_active_export_task", None)
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()


app.BrowserBackend.run_export_async = _run_export_with_task_handle
app.BrowserBackend.stop_async = _fast_stop_async


# ---------- Responsive / scrollable UI ----------
_original_window_init = app.MainWindow.__init__


def _reflow_field_box(window):
    field_boxes = getattr(window, "field_boxes", {})
    if not field_boxes:
        return

    first = next(iter(field_boxes.values()), None)
    if first is None or first.parentWidget() is None:
        return

    parent = first.parentWidget()
    layout = parent.layout()
    if layout is None:
        return

    width = window.width()
    if width < 760:
        cols = 2
    elif width < 1050:
        cols = 3
    else:
        cols = 5

    if getattr(window, "_field_cols", None) == cols:
        return
    window._field_cols = cols

    widgets = list(field_boxes.values())
    for w in widgets:
        layout.removeWidget(w)
    for i, w in enumerate(widgets):
        layout.addWidget(w, i // cols, i % cols)


def _apply_responsive_mode(window):
    splitters = window.findChildren(app.QSplitter)
    if splitters:
        main_splitter = splitters[0]
        desired = app.Qt.Vertical if window.width() < 980 else app.Qt.Horizontal
        if main_splitter.orientation() != desired:
            main_splitter.setOrientation(desired)
            if desired == app.Qt.Vertical:
                main_splitter.setSizes([620, 300])
            else:
                main_splitter.setSizes([780, 380])

    _reflow_field_box(window)


def _responsive_window_init(self, *args, **kwargs):
    _original_window_init(self, *args, **kwargs)

    # The previous fixed minimum size caused controls to be squeezed/off-screen
    # on 1366x768 and smaller displays.
    self.setMinimumSize(680, 520)

    screen = app.QApplication.primaryScreen()
    if screen is not None:
        geo = screen.availableGeometry()
        target_w = min(1180, max(680, int(geo.width() * 0.92)))
        target_h = min(820, max(520, int(geo.height() * 0.90)))
        self.resize(target_w, target_h)

    old_root = self.takeCentralWidget()
    scroll = app.QScrollArea(self)
    scroll.setObjectName("MainScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(app.QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(app.Qt.ScrollBarAsNeeded)
    scroll.setVerticalScrollBarPolicy(app.Qt.ScrollBarAsNeeded)
    scroll.setWidget(old_root)
    self.setCentralWidget(scroll)
    self._responsive_scroll = scroll

    # Let the query and log areas shrink; scrolling the whole page is preferable
    # to forcing the window beyond the physical screen.
    try:
        self.query.setMinimumHeight(120)
        self.log.setMinimumHeight(120)
    except Exception:
        pass

    _apply_responsive_mode(self)


_original_resize_event = getattr(app.MainWindow, "resizeEvent", None)


def _responsive_resize_event(self, event):
    if _original_resize_event is not None:
        try:
            _original_resize_event(self, event)
        except Exception:
            pass
    _apply_responsive_mode(self)


def _fast_stop_button(self):
    self.status.setText("正在立即停止…")
    self.stop.setEnabled(False)
    self.backend.submit(self.backend.stop_async())


app.MainWindow.__init__ = _responsive_window_init
app.MainWindow.resizeEvent = _responsive_resize_event
app.MainWindow.stop_export = _fast_stop_button


def main():
    app.main()


if __name__ == "__main__":
    main()
