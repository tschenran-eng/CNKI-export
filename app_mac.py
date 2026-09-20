from __future__ import annotations

import os
import sys
from pathlib import Path

import app_responsive

app = app_responsive.app


async def mac_launch(self) -> None:
    """Launch a persistent local Chromium-family browser on macOS."""
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

    # Playwright branded channels are the cleanest option when installed.
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

    # Explicit macOS application paths cover cases where Playwright channel
    # discovery does not find a locally installed browser.
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


def main() -> None:
    # Finder-launched apps may inherit an inconvenient working directory.
    # Put the default workspace under the user's home directory instead.
    try:
        os.chdir(Path.home())
    except Exception:
        pass

    app.RobustNativeExporter.launch = mac_launch
    app.main()


if __name__ == "__main__":
    main()
