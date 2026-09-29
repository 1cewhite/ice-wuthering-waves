"""JS 桥 / 工具条真实可用性验证（关键回归测试）。

这个脚本回答一个具体问题：**从页面里的 JS，能不能真的驱动悬浮窗移动/缩放/穿透？**

做法是直接向运行中的悬浮窗注入探测脚本，让页面自己去调用
``window.pywebview.api.ui(...)``，然后检查：
- 桥对象存在，且 ``ui`` 可被调用；
- 点击「穿透」按钮后主进程确实收到了 ``click_through=True``；
- 拖动序列（drag_start / drag_move / drag_end）真的改变了窗口几何。

用法：
    .\\.venv\\Scripts\\python.exe tests/floating_browser_bridge.py
"""

from __future__ import annotations

import json
import os
import sys
import time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from src.gui.floating_browser.browser import FloatingBrowser  # noqa: E402

# 用一个真实加载的 data: 页面，确保 loaded 事件正常触发
DATA_URL = (
    "data:text/html;charset=utf-8,"
    "%3C!DOCTYPE%20html%3E%3Chtml%3E%3Chead%3E%3Cmeta%20charset%3D%22utf-8%22%3E"
    "%3Ctitle%3EBridge%20Test%3C/title%3E%3C/head%3E"
    "%3Cbody%20style%3D%22margin:0;background:%23101216%22%3E"
    "%3Cvideo%20id%3D%22v%22%20muted%20playsinline%3E%3C/video%3E"
    "%3C/body%3E%3C/html%3E"
)


def main() -> int:
    if not FloatingBrowser.webview_available():
        print("WebView2 不可用，跳过")
        return 0

    results: list[str] = []
    failures = 0

    def report(ok: bool, message: str) -> None:
        nonlocal failures
        results.append(f"{'[OK]  ' if ok else '[FAIL]'} {message}")
        if not ok:
            failures += 1

    browser = FloatingBrowser(
        url=DATA_URL,
        width=560,
        height=340,
        log_info=lambda message: print(f"  [browser] {message}"),
    )
    if not browser.start(wait_ready=25.0) or browser.degraded:
        print(f"[FAIL] 未能进入内嵌模式: {browser.last_error}")
        browser.stop()
        return 1

    # 等页面与工具条就绪（而非盲目 sleep）
    deadline = time.time() + 15.0
    ready_probe = None
    while time.time() < deadline:
        ready_probe = browser.probe_js(
            "!!document.getElementById('__ok_xbar')", timeout=3.0
        )
        if ready_probe is True:
            break
        time.sleep(0.5)
    report(ready_probe is True, f"工具条在 15 秒内出现（实际 {ready_probe}）")

    time.sleep(1.0)

    # ---- 0. 窗口必须真实可见（曾因手改 WS_CAPTION 导致 IsWindowVisible 恒 False）----
    visible = None
    if os.name == "nt":
        import ctypes
        _user32 = ctypes.windll.user32
        _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        _user32.IsWindowVisible.restype = ctypes.c_int
        child = browser.inspect(timeout=4.0)
        hwnd = child.get("hwnd") if isinstance(child, dict) else 0
        if hwnd:
            deadline = time.time() + 5.0
            while time.time() < deadline:
                visible = bool(_user32.IsWindowVisible(hwnd))
                if visible:
                    break
                time.sleep(0.3)
    report(visible is True, f"悬浮窗真实可见（IsWindowVisible={visible}）")

    # ---- 1. 桥与工具条是否存在 ----
    probe_raw = browser.probe_js(
        "JSON.stringify({"
        "  bridge: !!(window.pywebview && window.pywebview.api),"
        "  ui: !!(window.pywebview && window.pywebview.api && window.pywebview.api.ui),"
        "  bar: !!document.getElementById('__ok_xbar'),"
        "  grips: document.querySelectorAll('.ok-resize').length,"
        "  ct: !!document.getElementById('__ok_ct'),"
        "  close: !!document.getElementById('__ok_close'),"
        "  pinSvg: !!(document.getElementById('__ok_top') && document.getElementById('__ok_top').querySelector('svg')),"
        "  minSvg: !!(document.getElementById('__ok_hide') && document.getElementById('__ok_hide').querySelector('svg')),"
        "  closeSvg: !!(document.getElementById('__ok_close') && document.getElementById('__ok_close').querySelector('svg'))"
        "})"
    )
    # probe_js 对 JSON.stringify 的结果返回字符串，需要再解析一次
    probe = None
    if isinstance(probe_raw, str):
        try:
            probe = json.loads(probe_raw)
        except Exception:
            probe = None
    elif isinstance(probe_raw, dict):
        probe = probe_raw
    report(probe is not None, f"可在悬浮窗内执行 JS 探测: {probe_raw}")
    if isinstance(probe, dict):
        report(probe.get("bridge") is True, "window.pywebview 存在")
        report(probe.get("ui") is True, "window.pywebview.api.ui 可调用（JS 桥可用）")
        report(probe.get("bar") is True, "工具条 #__ok_xbar 已注入")
        report(probe.get("grips") == 7, f"缩放把手数量为 7（实际 {probe.get('grips')}）")
        report(probe.get("close") is True, "工具条含关闭按钮 #__ok_close")
        report(probe.get("pinSvg") is True, "置顶按钮为图钉图标（SVG）")
        report(probe.get("minSvg") is True, "隐藏按钮为横杠图标（SVG）")
        report(probe.get("closeSvg") is True, "关闭按钮为图标（SVG）")

    # ---- 2. 穿透：真实点击「穿透」按钮 ----
    browser.set_click_through(False)
    time.sleep(0.6)
    browser.probe_js("document.getElementById('__ok_ct').click(); true;")
    time.sleep(1.2)
    report(browser.click_through_enabled, "点击「穿透」按钮后主进程侧状态变为 True")

    # 穿透状态下工具条应被隐藏，否则用户点不到「恢复」
    hidden = None
    deadline = time.time() + 6.0
    while time.time() < deadline:
        hidden = browser.probe_js(
            "!!document.getElementById('__ok_xbar') && "
            "getComputedStyle(document.getElementById('__ok_xbar')).display === 'none'",
            timeout=3.0,
        )
        if hidden is True:
            break
        time.sleep(0.4)
    report(hidden is True, "穿透时工具条自动隐藏（避免挡住下层点击）")

    # ---- 2b. 逃生通道：穿透后必须能通过 toggle_visible 恢复交互 ----
    browser.toggle_visible()
    time.sleep(1.2)
    report(browser.click_through_enabled is False, "toggle_visible 关闭穿透状态")
    restored = None
    deadline = time.time() + 6.0
    while time.time() < deadline:
        restored = browser.probe_js(
            "!!document.getElementById('__ok_xbar') && "
            "getComputedStyle(document.getElementById('__ok_xbar')).display !== 'none'",
            timeout=3.0,
        )
        if restored is True:
            break
        time.sleep(0.4)
    report(restored is True, "toggle_visible 后工具条重新可见（可再次操作）")

    browser.set_click_through(False)
    time.sleep(0.6)

    # ---- 3. 拖动：模拟真实鼠标序列 ----
    before = browser.geometry
    browser.probe_js(
        "var g = document.getElementById('__ok_drag');"
        "var opts = function (x, y) { return {button:0, screenX:x, screenY:y,"
        "  clientX:60, clientY:20, bubbles:true, cancelable:true}; };"
        "g.dispatchEvent(new MouseEvent('mousedown', opts(1000, 1000)));"
        "document.dispatchEvent(new MouseEvent('mousemove', opts(1070, 1040)));"
        "window.dispatchEvent(new MouseEvent('mouseup', opts(1070, 1040)));"
        "true;"
    )
    time.sleep(1.5)
    after = browser.geometry
    moved = (after[0] - before[0], after[1] - before[1])
    report(moved != (0, 0) or before[0] < 0,
           f"拖动后几何变化: {before} -> {after}（位移 {moved}）")
    report(browser.probe_js("!!window.__okSyncButtons") is True, "__okSyncButtons 已定义")

    # ---- 4. 关闭按钮：点击后应销毁窗口、子进程退出 ----
    browser.probe_js("document.getElementById('__ok_close').click(); true;")
    deadline = time.time() + 8.0
    closed = False
    while time.time() < deadline:
        if not browser.running:
            closed = True
            break
        time.sleep(0.3)
    report(closed, f"点击关闭按钮后子进程退出（running={browser.running}）")

    browser.stop()
    print("\n".join(results))
    print(f"\n==== 失败项合计: {failures} ====")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
