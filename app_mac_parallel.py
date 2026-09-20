from __future__ import annotations

import os
from pathlib import Path

import app_parallel

app = app_parallel.app


async def mac_launch(self) -> None:
    self.profile_dir.mkdir(parents=True, exist_ok=True)
    self._playwright = await app.async_playwright().start()

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

    errors: list[str] = []

    for channel, label in (
        ("msedge", "Microsoft Edge"),
        ("chrome", "Google Chrome"),
    ):
        try:
            self.context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                channel=channel,
                **common,
            )
            self.log(f"已使用本机 {label} 启动持久化浏览器会话。")
            return
        except Exception as exc:
            errors.append(f"{label}: {exc}")

    candidates = [
        (
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            "Microsoft Edge",
        ),
        (
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "Google Chrome",
        ),
        (
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            "Chromium",
        ),
        (
            Path.home()
            / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "Microsoft Edge",
        ),
        (
            Path.home()
            / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "Google Chrome",
        ),
    ]

    for executable, label in candidates:
        if not executable.exists():
            continue
        try:
            self.context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                executable_path=str(executable),
                **common,
            )
            self.log(f"已使用本机 {label} 启动持久化浏览器会话。")
            return
        except Exception as exc:
            errors.append(f"{label} ({executable}): {exc}")

    await self._playwright.stop()
    raise RuntimeError(
        "未找到可用的 Microsoft Edge、Google Chrome 或 Chromium。"
        "Mac 版目前需要至少安装其中一个浏览器；Safari 不能作为 Playwright Chromium 后端。"
        + ("\n" + " | ".join(errors) if errors else "")
    )


app.RobustNativeExporter.launch = mac_launch


def main() -> None:
    try:
        os.chdir(Path.home())
    except Exception:
        pass
    app_parallel.main()


if __name__ == "__main__":
    main()
