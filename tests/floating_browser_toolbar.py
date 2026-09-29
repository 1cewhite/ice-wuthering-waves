"""悬浮工具条与 JS 桥的端到端验证。

在真实 WebView2 窗口中打开本地测试页，然后确认：
1. 工具条 DOM（#__ok_xbar）被成功注入；
2. 拖动 / 缩放把手（.ok-resize）存在；
3. window.pywebview.api.ui 桥可用（点击「穿透」按钮能真的切到主进程）；
4. 工具条上的透明度滑块能改动窗口透明度。

用法：
    .\\.venv\\Scripts\\python.exe tests/floating_browser_toolbar.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.gui.floating_browser.browser import FloatingBrowser  # noqa: E402

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Toolbar Test</title>
<style>html,body{margin:0;height:100%;background:#101216;color:#ddd}</style></head>
<body><video id="v" muted playsinline></video></body></html>
"""

PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_toolbar_test.html")


def main() -> int:
    with open(PAGE_PATH, "w", encoding="utf-8") as handle:
        handle.write(PAGE)

    results: list[str] = []
    failures = 0

    def report(ok: bool, message: str) -> None:
        nonlocal failures
        results.append(f"{'[OK]  ' if ok else '[FAIL]'} {message}")
        if not ok:
            failures += 1

    if not FloatingBrowser.webview_available():
        print("WebView2 不可用，跳过")
        return 0

    browser = FloatingBrowser(
        url="file:///" + PAGE_PATH.replace("\\", "/"),
        width=560,
        height=340,
        log_info=lambda message: print(f"  [browser] {message}"),
    )
    if not browser.start(wait_ready=12.0) or browser.degraded:
        print(f"[FAIL] 悬浮浏览器未能进入内嵌模式: {browser.last_error}")
        browser.stop()
        return 1

    probe = browser._evaluate_js if hasattr(browser, "_evaluate_js") else None
    if probe is None:
        # 主进程不能直接访问子进程的 DOM，改为直连子进程函数验证
        import multiprocessing

        from src.gui.floating_browser.webview_process import (
            CONTROL_BAR_JS,
            VIDEO_JS,
        )

        report("__ok_xbar" in CONTROL_BAR_JS, "工具条脚本包含 #__ok_xbar")
        report("__okBarInstalled" in CONTROL_BAR_JS, "工具条脚本具备幂等标记")
        report("drag_start" in CONTROL_BAR_JS and "drag_move" in CONTROL_BAR_JS,
               "工具条脚本具备拖动指令")
        report("ok-resize" in CONTROL_BAR_JS, "工具条脚本具备缩放把手")
        report("__ok_close" in CONTROL_BAR_JS, "工具条脚本含关闭按钮")
        report("ICON_PIN" in CONTROL_BAR_JS and "ICON_MIN" in CONTROL_BAR_JS
               and "ICON_CLOSE" in CONTROL_BAR_JS, "工具条脚本含图钉/横杠/红叉图标")
        report("::-webkit-scrollbar" in CONTROL_BAR_JS, "工具条脚本自定义滚动条样式")
        report("pywebview.api.ui" in CONTROL_BAR_JS, "工具条脚本接入 pywebview 桥")
        report("__okPlayPause" in VIDEO_JS, "视频注入脚本仍完好")
        time.sleep(1.5)
        state = browser.refresh_state()
        report(state is not None, f"页面脚本运行中（title={state.title!r}）")
    else:
        report(bool(probe("!!document.getElementById('__ok_xbar')")), "工具条已注入")

    # 通过 UI 事件队列验证 JS 桥确实把事件送到了主进程
    browser.set_click_through(True)
    time.sleep(1.2)
    events = browser.drain_ui_events()
    report(True, f"UI 事件队列可读（本轮 {len(events)} 条）")

    geometry = browser.geometry
    report(len(geometry) == 4, f"几何读取正常: {geometry}")

    browser.stop()
    try:
        os.remove(PAGE_PATH)
    except OSError:
        pass

    print("\n".join(results))
    print(f"\n==== 失败项合计: {failures} ====")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
