"""悬浮浏览器功能自测脚本。

用法（在项目虚拟环境中运行）：
    .\\.venv\\Scripts\\python.exe tests/floating_browser_check.py
    .\\.venv\\Scripts\\python.exe tests/floating_browser_check.py --hotkeys
    .\\.venv\\Scripts\\python.exe tests/floating_browser_check.py --no-window

脚本会：
1. 校验纯逻辑（热键解析、尺寸/透明度裁剪、地址归一化）；
2. 创建内嵌 HTML 的本地测试页并打开悬浮窗口，验证尺寸、透明度与 JS 视频控制；
3. 可选验证全局热键注册。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.gui.floating_browser.browser import (  # noqa: E402
    BrowserState,
    FloatingBrowser,
    normalize_url,
)
from src.gui.floating_browser.hotkeys import HotkeyManager, parse_hotkey  # noqa: E402

TEST_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Floating Browser Test</title>
<style>html,body{margin:0;height:100%;background:#101216;color:#ddd;font-family:sans-serif}
video{width:100%;height:100%;object-fit:contain}</style></head>
<body>
<video id="v" muted playsinline></video>
<script>
// 构造一个带已知时长与可写 currentTime 的假 video，便于在没有真实媒体时校验控制逻辑
const v = document.getElementById('v');
Object.defineProperty(v, 'duration', { value: 300, writable: false });
let _t = 0;
Object.defineProperty(v, 'currentTime', {
  get: function () { return _t; },
  set: function (val) { _t = Math.max(0, Math.min(300, Number(val) || 0)); },
  configurable: true
});
v.pause = function () { this._paused = true; };
v.play = function () { this._paused = false; return Promise.resolve(); };
Object.defineProperty(v, 'paused', {
  get: function () { return this._paused !== false; },
  configurable: true
});
// 标记页面已就绪，方便测试判断
document.title = 'Floating Browser Test Ready';
</script>
</body></html>
"""

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_floating_browser_test.html")
MIRROR_HTML_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "_floating_browser_mirror.html"
)

# 模拟 B 站弹幕/字幕的 DOM：一条可见弹幕、一条**真滚动**的弹幕（验证速度估算+本地补帧）、
# 一条 opacity=0 的（播放器池化节点，不该被镜像）、一条字幕。
# 样式照抄 B 站实测值（SimHei / 700 / text-shadow 0 0 1px 黑），用于验证「样式与 B 站一致」。
# 字幕故意做成「整宽容器 + 居中文字」——B 站就是这样，直接用容器矩形画会偏，
# 必须用 Range 量出真实文字矩形（这也正是「字幕位置不对」的根因）。
DM_CSS = "font-family:SimHei;font-weight:700;text-shadow:rgb(0, 0, 0) 0px 0px 1px;"
MIRROR_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#101216}
@keyframes okroll { from { transform: translateX(600px); } to { transform: translateX(-120px); } }
.ok-rolling { animation: okroll 4s linear infinite; }
</style></head><body>
<video></video>
<div class="bili-danmaku-x-dm" style="position:absolute;left:40px;top:60px;font-size:20px;color:rgb(255,255,255);__DM_CSS__">可见弹幕</div>
<div class="bili-danmaku-x-dm ok-rolling" style="position:absolute;left:0;top:100px;font-size:20px;color:rgb(255,255,255);__DM_CSS__">滚动弹幕</div>
<div class="bili-danmaku-x-dm" style="position:absolute;left:200px;top:140px;font-size:20px;color:rgb(255,212,0);opacity:0;__DM_CSS__">不可见弹幕</div>
<div class="bili-danmaku-x-dm" style="position:absolute;left:40px;top:180px;font-size:20px;color:rgb(255,255,255);__DM_CSS__"></div>
<div class="bili-subtitle-x-subtitle-panel-text" style="position:absolute;left:0;top:300px;width:100%;text-align:center;font-size:20px;color:rgb(255,255,255);__DM_CSS__">测试字幕</div>
</body></html>
""".replace("__DM_CSS__", DM_CSS)


def write_mirror_page() -> str:
    with open(MIRROR_HTML_PATH, "w", encoding="utf-8") as handle:
        handle.write(MIRROR_HTML)
    return "file:///" + MIRROR_HTML_PATH.replace("\\", "/")


def write_test_page() -> str:
    with open(HTML_PATH, "w", encoding="utf-8") as handle:
        handle.write(TEST_HTML)
    return "file:///" + HTML_PATH.replace("\\", "/")


# 窗口过程必须保活：ctypes 回调对象一旦被 GC，系统再给这个窗口派消息
# （DestroyWindow 会发 WM_DESTROY）就会访问已释放的内存 —— 进程直接 access violation。
_FAKE_WNDPROC_REFS: list = []


def make_fake_game_window(x: int, y: int, width: int, height: int):
    """造一个窗口冒充「游戏窗口」，用于验证弹幕覆盖层的锚点。

    不需要消息泵：这里只用它的客户区矩形做几何断言。位置故意放到屏幕外，
    避免测试时真的有东西盖在桌面上。
    """
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = ctypes.c_void_p

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
            ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
            ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p),
        ]

    wndproc = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p
    )(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
    _FAKE_WNDPROC_REFS.append(wndproc)
    user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                                      ctypes.c_void_p]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    hmod = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p).value
    wc.hInstance = hmod
    wc.lpszClassName = "OKWWTestFakeGame"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(
        0, "OKWWTestFakeGame", "FakeGame", 0x80000000,  # WS_POPUP
        x, y, width, height, None, None, hmod, None,
    )
    user32.DestroyWindow.argtypes = [ctypes.c_void_p]
    return int(hwnd or 0), user32


def check_logic() -> int:
    print("[1] 纯逻辑检查")
    failures = 0

    cases = {
        "ctrl+shift+space": (6, 0x20),
        "ctrl+shift+right": (6, 0x27),
        "ctrl+shift+left": (6, 0x25),
        "ctrl+shift+up": (6, 0x26),
        "ctrl+shift+down": (6, 0x28),
        "ctrl+shift+m": (6, 0x4D),
        "ctrl+shift+h": (6, 0x48),
        "ctrl+shift+0": (6, 0x30),
        "f8": (0, 0x77),
        "ctrl+alt+space": (3, 0x20),
        "ctrl+alt+a+b": None,
        "none": None,
        "": None,
        "bad+key": None,
    }
    for text, expected in cases.items():
        got = parse_hotkey(text)
        if got != expected:
            print(f"  [FAIL] parse_hotkey({text!r}) = {got}, 期望 {expected}")
            failures += 1
    print(f"  热键解析: {len(cases)} 项, 失败 {sum(1 for t, e in cases.items() if parse_hotkey(t) != e)}")

    browser = FloatingBrowser(url="about:blank", width=99999, height=1, opacity=5.0)
    if browser.width != 3840 or browser.height != 140 or abs(browser.opacity - 1.0) > 1e-6:
        print(f"  [FAIL] 上限裁剪: {browser.width}x{browser.height} @{browser.opacity}")
        failures += 1
    browser = FloatingBrowser(url="about:blank", width=1, height=99999, opacity=0.01)
    if browser.width != 240 or browser.height != 2160 or abs(browser.opacity - 0.2) > 1e-6:
        print(f"  [FAIL] 下限裁剪: {browser.width}x{browser.height} @{browser.opacity}")
        failures += 1

    url_cases = {
        "": "https://www.bilibili.com/",
        "https://v.qq.com/": "https://v.qq.com/",
        "www.youtube.com": "https://www.youtube.com",
        "http://a.cn/x": "http://a.cn/x",
    }
    for raw, expected in url_cases.items():
        got = normalize_url(raw)
        if got != expected:
            print(f"  [FAIL] normalize_url({raw!r}) = {got}, 期望 {expected}")
            failures += 1

    state = BrowserState.from_payload(
        {"found": True, "paused": False, "current": 12.5, "duration": 300, "rate": 1.5}
    )
    if not state.found or state.paused or abs(state.current - 12.5) > 1e-6 or abs(state.rate - 1.5) > 1e-6:
        print(f"  [FAIL] BrowserState 解析: {state}")
        failures += 1
    if BrowserState.from_payload(None).found:
        print("  [FAIL] BrowserState 对 None 的容错")
        failures += 1

    print(f"  逻辑检查失败项: {failures}")
    return failures


def check_window(seconds: float) -> int:
    print("\n[2] 窗口能力检查")
    failures = 0
    available = FloatingBrowser.webview_available()
    print(f"  WebView2 可用: {available}")

    source = write_test_page() if available else "https://www.bilibili.com/"
    browser = FloatingBrowser(
        url=source,
        width=640,
        height=360,
        opacity=0.85,
        log_info=lambda message: print(f"  [browser] {message}"),
    )
    ok = browser.start(wait_ready=seconds)
    print(f"  start() -> {ok}, native_mode={browser.native_mode}, degraded={browser.degraded}")

    if browser.degraded:
        print(f"  [INFO] 降级模式: {browser.last_error}")
        browser.stop()
        return failures
    if not browser.running:
        print("  [FAIL] 悬浮浏览器未运行")
        browser.stop()
        return failures + 1

    # 尺寸 / 透明度
    browser.set_size(800, 450)
    if (browser.width, browser.height) != (800, 450):
        print(f"  [FAIL] set_size: {(browser.width, browser.height)}")
        failures += 1
    else:
        print("  [OK] set_size -> 800x450")

    browser.set_opacity(0.6)
    if abs(browser.opacity - 0.6) > 1e-6:
        print(f"  [FAIL] set_opacity: {browser.opacity}")
        failures += 1
    else:
        print("  [OK] set_opacity -> 0.6")

    time.sleep(1.0)
    state = browser.refresh_state()
    print(f"  [state] found={state.found} paused={state.paused} "
          f"current={state.current} duration={state.duration} rate={state.rate}")
    if not state.found:
        print("  [WARN] 未识别到 video 元素，跳过视频控制检查（可能是页面尚未加载完成）")
    else:
        browser.set_rate(2.0)
        time.sleep(0.6)
        state = browser.refresh_state()
        if abs(state.rate - 2.0) > 0.01:
            print(f"  [FAIL] set_rate -> {state.rate}")
            failures += 1
        else:
            print("  [OK] set_rate -> 2.0x")

        browser.set_volume(0.5)
        time.sleep(0.5)
        browser.volume_up()
        time.sleep(0.6)
        up = browser.refresh_state().volume
        if up <= 0.5:
            print(f"  [FAIL] volume_up 未提升: {up}")
            failures += 1
        else:
            print(f"  [OK] volume_up -> {up:.2f}")

        browser.volume_down()
        time.sleep(0.6)
        down = browser.refresh_state().volume
        if down >= up:
            print(f"  [FAIL] volume_down 未降低: {down}")
            failures += 1
        else:
            print(f"  [OK] volume_down -> {down:.2f}")

        browser.seek(10)
        time.sleep(0.6)
        state = browser.refresh_state()
        if abs(state.current - 10.0) > 1.0:
            print(f"  [FAIL] seek(+10) -> {state.current}")
            failures += 1
        else:
            print(f"  [OK] seek(+10s) -> {state.current:.2f}s")

        before = browser.refresh_state().paused
        browser.play_pause()
        time.sleep(0.6)
        after = browser.refresh_state().paused
        if before == after:
            print(f"  [FAIL] play_pause 状态未变化: {before}")
            failures += 1
        else:
            print(f"  [OK] play_pause {before} -> {after}")

    browser.handle_action("toggle_click_through")
    time.sleep(0.4)
    browser.handle_action("toggle_click_through")
    print("  [OK] toggle_click_through 往返调用")

    # ---- 鼠标穿透 / 几何 ----
    browser.set_click_through(True)
    if not browser.click_through_enabled:
        print("  [FAIL] set_click_through(True) 未生效")
        failures += 1
    else:
        print("  [OK] set_click_through(True)")
    browser.toggle_click_through()
    if browser.click_through_enabled:
        print("  [FAIL] toggle_click_through 未切回")
        failures += 1
    else:
        print("  [OK] toggle_click_through -> False")

    geometry = browser.geometry
    if len(geometry) != 4 or geometry[2] != 800 or geometry[3] != 450:
        print(f"  [FAIL] geometry 与 set_size 不一致: {geometry}")
        failures += 1
    else:
        print(f"  [OK] geometry -> {geometry}")

    # ---- 子进程内部状态自检（hwnd 必须解析成功，否则拖动/穿透全废）----
    child = browser.inspect(timeout=4.0)
    if isinstance(child, dict) and child.get("hwnd"):
        print(f"  [OK] 子进程已解析窗口句柄 hwnd={child['hwnd']}")
    else:
        print(f"  [FAIL] 子进程未解析到窗口句柄（拖动/缩放/穿透都会失效）: {child}")
        failures += 1

    # ---- 模拟工具条拖动（走子进程真实 Win32 路径）----
    before = browser.geometry
    browser.probe_js(
        "var g=document.getElementById('__ok_drag');"
        "var mk=function(t,x,y){return new MouseEvent(t,{button:0,screenX:x,screenY:y,"
        "clientX:60,clientY:20,bubbles:true,cancelable:true});};"
        "g.dispatchEvent(mk('mousedown',1000,1000));"
        "document.dispatchEvent(mk('mousemove',1040,1020));"
        "window.dispatchEvent(mk('mouseup',1040,1020));"
        "true;",
        timeout=4.0,
    )
    time.sleep(1.2)
    after = browser.geometry
    if (after[0], after[1]) != (before[0], before[1]):
        print(f"  [OK] 工具条拖动改变窗口位置: {before} -> {after}")
    else:
        print(f"  [FAIL] 工具条拖动未改变窗口位置: {before} -> {after}")
        failures += 1

    browser.stop()
    print("  [OK] stop")
    return failures


def _hotkey_occupied(modifiers: int, key_code: int) -> bool:
    """探测某个组合是否已被其它程序占用（只认「已注册」这一种失败原因）。"""
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterHotKey.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
    ]
    user32.RegisterHotKey.restype = ctypes.c_int
    user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
    hotkey_id = 0xC1FF
    ctypes.set_last_error(0)
    ok = user32.RegisterHotKey(None, hotkey_id, modifiers | 0x4000, key_code)
    error = ctypes.get_last_error()
    if ok:
        user32.UnregisterHotKey(None, hotkey_id)
        return False
    return error == 1409  # ERROR_HOTKEY_ALREADY_REGISTERED


def check_hold_hook() -> int:
    """校验「按住3倍速」的低级键盘钩子能安装并真正触发。

    这里必须验证「触发」而不只是「安装」：历史上钩子安装失败（ok 框架把
    GetModuleHandleW.restype 设成 32 位 c_long，模块句柄被截断导致
    SetWindowsHookExW 失败），表现为「按住没反应」，且异常发生在子线程里，
    主程序完全无感知。
    """
    print("\n[4] 按住3倍速（低级键盘钩子）检查")
    if os.name != "nt":
        print("  非 Windows，跳过")
        return 0

    from src.gui.floating_browser.hotkeys import HoldKeyManager, _user32

    events: list[str] = []
    manager = HoldKeyManager(
        on_down=lambda: events.append("down"), on_up=lambda: events.append("up")
    )
    manager.set_hotkey("alt+f")
    manager.start()
    deadline = time.time() + 2.0
    while time.time() < deadline and not manager.installed and manager.running:
        time.sleep(0.05)

    failures = 0
    if not manager.installed:
        print(f"  [FAIL] 钩子安装失败: {manager.last_error or '未知原因'}")
        failures += 1
    else:
        print(f"  [OK] 钩子安装成功（hook={manager._hook}）")
        _user32.keybd_event(0x12, 0, 0, 0)  # Alt down
        _user32.keybd_event(0x46, 0, 0, 0)  # F down
        time.sleep(0.25)
        _user32.keybd_event(0x46, 0, 2, 0)  # F up
        _user32.keybd_event(0x12, 0, 2, 0)  # Alt up
        deadline = time.time() + 2.0
        while time.time() < deadline and len(events) < 2:
            time.sleep(0.05)
        if events[:2] == ["down", "up"]:
            print(f"  [OK] alt+f 触发按下/松开: {events}")
        else:
            print(f"  [FAIL] 钩子未收到按下/松开事件（实际 {events}）")
            failures += 1

    manager.stop()
    return failures


def check_hotkeys() -> int:
    print("\n[3] 热键注册检查")
    if os.name != "nt":
        print("  非 Windows，跳过")
        return 0

    from src.gui.floating_browser.hotkeys import HOTKEY_ACTIONS, parse_hotkey

    # 用真实默认值绑定（而不是写死字符串），这样改默认键位后测试会同步校验
    bindings = {
        action: {"hotkey": meta["default"], "value": meta.get("step")}
        for action, meta in HOTKEY_ACTIONS.items()
        if action != "hold_fast_forward"  # 按住类走键盘钩子，由 check_hold_hook 校验
    }
    print(f"  默认键位: " + ", ".join(f"{a}={meta['default']}" for a, meta in HOTKEY_ACTIONS.items()))
    parsed = parse_hotkey("`")
    failed = 0
    if parsed == (0, 0xC0):
        print("  [OK] 反引号单个按键可解析 -> (0, 0xC0)")
    else:
        print(f"  [FAIL] 反引号解析异常: {parsed}")
        failed += 1

    events: list[str] = []
    manager = HotkeyManager(dispatch=lambda action, value: events.append(action))
    manager.start()
    results = manager.bind_all(bindings)
    for action, ok in results.items():
        hotkey = bindings[action]["hotkey"]
        if ok:
            print(f"  [OK] 注册 {action}: {ok}")
            continue
        # 失败要区分「被别的程序占用」（环境问题）和「其它原因」（代码问题）
        parsed = parse_hotkey(hotkey)
        if parsed and _hotkey_occupied(parsed[0], parsed[1]):
            print(f"  [WARN] {action} -> {hotkey} 已被其它程序占用（环境冲突，非代码问题）")
        else:
            print(f"  [FAIL] {action} -> {hotkey} 注册失败，且并非「已被占用」")
            failed += 1
    if manager.failed_bindings:
        print(f"  被其它程序占用的热键: {manager.failed_bindings}")

    # 关键回归：验证 WM_HOTKEY 能真正触发 dispatch（而不只是注册成功）。
    # 曾因 RegisterHotKey 在错误线程调用，导致「注册成功但按键无效」。
    import ctypes

    _user32 = ctypes.windll.user32
    _user32.PostThreadMessageW.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    _user32.PostThreadMessageW.restype = ctypes.c_int
    _thread_id = getattr(manager, "_win_thread_id", None)
    _hotkey_id = None
    with manager._lock:
        if manager._registered:
            _hotkey_id = next(iter(manager._registered))
    if _thread_id and _hotkey_id:
        _user32.PostThreadMessageW(_thread_id, 0x0312, _hotkey_id, 0)  # WM_HOTKEY
        deadline = time.time() + 3.0
        while time.time() < deadline and not events:
            time.sleep(0.05)
        if events:
            print(f"  [OK] WM_HOTKEY 触发 dispatch: {events}")
        else:
            print("  [FAIL] 热键注册成功但 WM_HOTKEY 未触发 dispatch（线程队列不匹配）")
            failed += 1
    else:
        print(f"  [WARN] 无法验证触发链路（thread_id={_thread_id} hotkey_id={_hotkey_id}）")

    manager.stop()
    print("  已注销全部热键")
    return failed


def check_log_switch() -> int:
    """日志总开关：关掉后悬浮浏览器不该往控制台写任何东西，打开后要能全部输出。"""
    print("\n[6] 日志总开关检查")
    from src.gui.floating_browser import log as fb_log

    calls: list = []

    class _Recorder:
        def info(self, message):
            calls.append(("info", message))

        def debug(self, message):
            calls.append(("debug", message))

        def warning(self, message):
            calls.append(("warning", message))

        def error(self, message):
            calls.append(("error", message))

    original = fb_log._logger
    fb_log._logger = _Recorder()
    failures = 0
    try:
        fb_log.set_verbose(False)
        for emit in (fb_log.info, fb_log.debug, fb_log.warning, fb_log.error):
            emit("should-be-silent")
        if calls:
            print(f"  [FAIL] 关闭时仍有输出: {calls}")
            failures += 1
        else:
            print("  [OK] 关闭时 info/debug/warning/error 全部静默")

        fb_log.set_verbose(True)
        for emit in (fb_log.info, fb_log.debug, fb_log.warning, fb_log.error):
            emit("should-be-visible")
        if len(calls) == 4:
            print("  [OK] 打开时四个级别都能输出")
        else:
            print(f"  [FAIL] 打开时输出数量异常: {len(calls)}")
            failures += 1

        # 子进程诊断（stderr）也要受同一个开关控制
        import contextlib
        import io

        fb_log.set_verbose(False)
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            fb_log.trace("silent-trace")
        silent = buffer.getvalue() == ""

        fb_log.set_verbose(True)
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            fb_log.trace("visible-trace")
        loud = "visible-trace" in buffer.getvalue()

        if silent and loud:
            print("  [OK] 子进程 trace 同样受开关控制")
        else:
            print(f"  [FAIL] trace 开关异常: silent={silent} loud={loud}")
            failures += 1
    finally:
        fb_log.set_verbose(False)
        fb_log._logger = original
    return failures


def check_danmaku_mirror(seconds: float) -> int:
    """[7] 弹幕/字幕映射：覆盖层窗口、置顶/穿透、几何跟随、内容与开关。"""
    print("\n[7] 弹幕/字幕映射检查")
    if os.name != "nt" or not FloatingBrowser.webview_available():
        print("  非 Windows 或 WebView2 不可用，跳过")
        return 0

    import ctypes

    from src.gui.floating_browser.hotkeys import HOTKEY_ACTIONS

    failures = 0
    if "toggle_mirror" in HOTKEY_ACTIONS:
        print(f"  [OK] 热键动作 toggle_mirror 已定义（默认 {HOTKEY_ACTIONS['toggle_mirror']['default']}）")
    else:
        print("  [FAIL] 缺少 toggle_mirror 热键动作")
        failures += 1

    browser = FloatingBrowser(
        url=write_mirror_page(), width=620, height=400, opacity=1.0,
        log_info=lambda message: None,
    )
    if not browser.start(wait_ready=seconds) or not browser.running:
        print("  [FAIL] 悬浮浏览器未运行")
        browser.stop()
        return failures + 1

    deadline = time.time() + 15.0
    while time.time() < deadline:
        if browser.probe_js("!!document.getElementById('__ok_xbar')", timeout=3) is True:
            break
        time.sleep(0.4)
    time.sleep(0.8)

    info = browser.mirror_debug(timeout=5.0)
    if isinstance(info, dict) and info.get("on") is False and info.get("overlay") is None:
        print("  [OK] 默认不开启映射（没有覆盖层窗口）")
    else:
        print(f"  [FAIL] 默认状态异常: {info}")
        failures += 1

    browser.set_mirror(True)
    time.sleep(2.0)
    info = browser.mirror_debug(timeout=5.0) or {}
    overlay = info.get("overlay") or {}
    window_rect = list(info.get("window_rect") or [0, 0, 0, 0])
    game_rect = list(info.get("game_rect") or [])
    game_on = bool(info.get("game_hwnd") and game_rect)
    # 「尺寸 = 游戏窗口大小」：整块铺满游戏画面；没有游戏窗口就跟随悬浮窗。
    expected = game_rect if game_on else window_rect
    where = f"游戏画面 {expected}" if game_on else f"悬浮窗（没有游戏窗口，回退）{expected}"
    checks = [
        (info.get("on") is True, "开启后子进程状态为「映射中」"),
        (bool(overlay.get("hwnd")), f"覆盖层窗口已创建 hwnd={overlay.get('hwnd')}"),
        (overlay.get("shown") is True, "覆盖层已显示"),
        (list(overlay.get("geometry") or []) == expected,
         f"覆盖层位置+尺寸 = {where}，实际 {overlay.get('geometry')}"),
        (overlay.get("last_draw", [0, 0, 0, False])[2] >= 1,
         f"只镜像了真实可见的弹幕（opacity=0 与空节点已过滤）-> {overlay.get('last_draw')}"),
        (overlay.get("last_draw", [0, 0, 0, False])[3] is True, "字幕也被采集到了"),
    ]
    for ok, label in checks:
        print(f"  {'[OK]' if ok else '[FAIL]'} {label}")
        if not ok:
            failures += 1

    # 「滚动弹幕要平滑」：页面给出每条弹幕的横向速度，覆盖层据此按 60fps 本地补帧
    rolling = [item for item in (overlay.get("frame_items") or [])
               if abs(float(item.get("vx") or 0)) >= 20]
    if rolling:
        sample = rolling[0]
        print(f"  [OK] 滚动弹幕速度已估算 vx={round(float(sample.get('vx')), 1)}px/s"
              f"（{sample.get('t')!r}）")
        print(f"  {'[OK]' if overlay.get('animating') is True else '[FAIL]'} "
              f"覆盖层进入补帧模式（animating={overlay.get('animating')}）")
        if overlay.get("animating") is not True:
            failures += 1
        # 用帧计数器算实际帧率（硬指标）。注意别频繁查询 —— 每次查询都会抢 GIL，
        # 把待测的帧率本身压下去，所以只在头尾各取一次计数。
        first = browser.mirror_debug(timeout=3.0) or {}
        start_frames = int((first.get("overlay") or {}).get("frames") or 0)
        started = time.time()
        time.sleep(0.8)
        second = browser.mirror_debug(timeout=3.0) or {}
        second_overlay = second.get("overlay") or {}
        span = time.time() - started
        drawn = int(second_overlay.get("frames") or 0) - start_frames
        fps = drawn / span if span > 0 else 0
        best_dt = float(second_overlay.get("frame_dt") or 0)
        for _ in range(3):
            time.sleep(0.02)
            probe = browser.mirror_debug(timeout=3.0) or {}
            best_dt = max(best_dt, float((probe.get("overlay") or {}).get("frame_dt") or 0))
        print(f"  {'[OK]' if fps >= 40 else '[FAIL]'} 实际重画帧率 {fps:.0f}fps"
              f"（{drawn} 帧 / {span * 1000:.0f}ms；页面只推 ~16fps，其余靠本地补帧）")
        if fps < 40:
            failures += 1
        print(f"  {'[OK]' if best_dt > 0 else '[FAIL]'} 两次推送之间确实在本地外推位置 "
              f"(frame_dt 最大 {round(best_dt * 1000)}ms)")
        if best_dt <= 0:
            failures += 1
        print(f"  [OK] 补帧单帧绘制耗时 {second_overlay.get('draw_ms')}ms"
              f"（60fps 的预算 16.7ms）")
    else:
        print(f"  [FAIL] 没估算出滚动速度（frame_items={overlay.get('frame_items')}）")
        failures += 1

    # 「位置映射、样式不动」：位置按 覆盖层÷视口 映射，字号等样式保持站点原值
    raw = (overlay.get("raw_items") or [{}])[0]
    mapped = (overlay.get("frame_items") or [{}])[0]
    viewport = list(overlay.get("viewport") or [0, 0])
    scale = list(overlay.get("scale") or [0, 0])
    if raw.get("x") is not None and mapped.get("x") is not None and viewport[0]:
        want = [expected[2] / viewport[0], expected[3] / viewport[1]]
        scale_ok = abs(scale[0] - want[0]) < 0.02 and abs(scale[1] - want[1]) < 0.02
        print(f"  {'[OK]' if scale_ok else '[FAIL]'} 位置映射比例 = 覆盖层÷视口 "
              f"{[round(v, 3) for v in want]}，实际 {[round(v, 3) for v in scale]}")
        if not scale_ok:
            failures += 1
        pos_ok = (abs(mapped.get("x", 0) - round((raw.get("x") or 0) * scale[0])) <= 1
                  and abs(mapped.get("y", 0) - round((raw.get("y") or 0) * scale[1])) <= 1)
        print(f"  {'[OK]' if pos_ok else '[FAIL]'} 位置按比例映射 "
              f"({raw.get('x')},{raw.get('y')}) -> ({mapped.get('x')},{mapped.get('y')})")
        if not pos_ok:
            failures += 1
        style_ok = (mapped.get("s") == raw.get("s")
                    and mapped.get("c") == raw.get("c")
                    and mapped.get("t") == raw.get("t"))
        print(f"  {'[OK]' if style_ok else '[FAIL]'} 字体样式不动：字号 "
              f"{raw.get('s')}px -> {mapped.get('s')}px、颜色/文本原样")
        if not style_ok:
            failures += 1
    else:
        print("  [SKIP] 没采到弹幕，跳过位置映射断言")

    # 字幕：要用 Range 量出的**真实文字矩形**居中绘制（容器是整宽的，直接用容器矩形会画偏）
    subtitle = overlay.get("raw_subtitle") or {}
    if subtitle.get("t"):
        text_rect_ok = (subtitle.get("center") is True
                        and subtitle.get("cx") is not None
                        and int(subtitle.get("w") or 0) > 0
                        and int(subtitle.get("w")) < int(window_rect[2] or 0))
        print(f"  {'[OK]' if text_rect_ok else '[FAIL]'} 字幕用真实文字矩形并居中 "
              f"(cx={subtitle.get('cx')}, w={subtitle.get('w')}, 容器宽={window_rect[2]})")
        if not text_rect_ok:
            failures += 1
        center_ok = abs(int(subtitle.get("cx") or 0) - int(window_rect[2] or 0) / 2) < 40
        print(f"  {'[OK]' if center_ok else '[FAIL]'} 字幕文字中心 ≈ 视频中线 "
              f"({subtitle.get('cx')} vs {int(window_rect[2] or 0) / 2:.0f})")
        if not center_ok:
            failures += 1
    else:
        print("  [SKIP] 没采到字幕，跳过字幕对中位断言")

    # 「样式与 B 站一致」：字体/字重/描边都取自站点的计算样式
    style = overlay.get("style") or {}
    outline = int(overlay.get("outline") or 0)
    from src.gui.floating_browser.danmaku_overlay import MAX_OUTLINE

    want_outline = max(1, min(MAX_OUTLINE, int(round(float(style.get('blur') or 1)))))
    for ok, label in [
        (style.get("family") == "SimHei",
         f"字体取自站点样式（B 站是 SimHei），实际 {style.get('family')!r}"),
        (style.get("bold") is True, f"字重取自站点样式（B 站是 700），实际 {style.get('bold')}"),
        (outline == want_outline,
         f"描边 = 站点 text-shadow = {outline}px（期望 {want_outline}px）"),
    ]:
        print(f"  {'[OK]' if ok else '[FAIL]'} {label}")
        if not ok:
            failures += 1

    # 「锚到游戏窗口」：推一个假游戏窗口句柄下去，覆盖层必须整块铺到它上面
    # 位置放在屏幕外，避免测试时真的有窗口盖在桌面上。
    fake_hwnd, fake_user32 = make_fake_game_window(2600, 120, 800, 600)
    if fake_hwnd:
        browser.set_game_hwnd(fake_hwnd)
        time.sleep(2.0)
        anchored = browser.mirror_debug(timeout=5.0) or {}
        overlay2 = anchored.get("overlay") or {}
        rect2 = list(overlay2.get("rect") or [])
        viewport2 = list(overlay2.get("viewport") or [0, 0])
        scale2 = list(overlay2.get("scale") or [0, 0])
        raw2 = (overlay2.get("raw_items") or [{}])[0]
        mapped2 = (overlay2.get("frame_items") or [{}])[0]
        style2 = overlay2.get("style") or {}
        want_scale = [800 / max(1, viewport2[0]), 600 / max(1, viewport2[1])]
        for ok, label in [
            (anchored.get("game_hwnd") == fake_hwnd,
             f"子进程接受了游戏窗口句柄 {fake_hwnd}"),
            ((anchored.get("game_rect") or [])[:2] == [2600, 120],
             f"读到的游戏画面原点 = (2600,120)，实际 {(anchored.get('game_rect') or [])[:2]}"),
            (rect2 == [2600, 120, 800, 600],
             f"覆盖层铺满整个游戏窗口（尺寸=游戏窗口）{rect2}"),
            (abs(scale2[0] - want_scale[0]) < 0.02 and abs(scale2[1] - want_scale[1]) < 0.02,
             f"位置映射比例 = 800x600 ÷ 视口 {viewport2} = {[round(v, 3) for v in want_scale]}，"
             f"实际 {[round(v, 3) for v in scale2]}"),
            (raw2.get("s") and mapped2.get("s") and mapped2["s"] == raw2["s"],
             f"字号等样式不动：{raw2.get('s')}px -> {mapped2.get('s')}px"),
            (style2.get("family") == "SimHei" and style2.get("bold") is True,
             f"画字用站点样式 family={style2.get('family')!r} bold={style2.get('bold')}"),
        ]:
            print(f"  {'[OK]' if ok else '[FAIL]'} {label}")
            if not ok:
                failures += 1

        # 游戏窗口移动后，覆盖层要跟着走
        fake_user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        fake_user32.SetWindowPos(fake_hwnd, None, 2600, 800, 800, 600, 0x0010 | 0x0004)
        time.sleep(2.0)
        moved = browser.mirror_debug(timeout=5.0) or {}
        rect3 = list(((moved.get("overlay") or {}).get("rect") or []))
        if rect3[:2] == [2600, 800]:
            print("  [OK] 游戏窗口移动后覆盖层跟随")
        else:
            print(f"  [FAIL] 覆盖层未跟随游戏窗口移动: {rect3[:2]}")
            failures += 1

        # 游戏窗口改尺寸后，覆盖层尺寸与位置映射比例都要跟着变
        fake_user32.SetWindowPos(fake_hwnd, None, 2600, 800, 1000, 500, 0x0010 | 0x0004)
        time.sleep(2.0)
        resized = browser.mirror_debug(timeout=5.0) or {}
        rect5 = list(((resized.get("overlay") or {}).get("rect") or []))
        scale5 = list(((resized.get("overlay") or {}).get("scale") or [0, 0]))
        viewport5 = list(((resized.get("overlay") or {}).get("viewport") or [0, 0]))
        if rect5 == [2600, 800, 1000, 500] and abs(scale5[1] - 500 / max(1, viewport5[1])) < 0.02:
            print("  [OK] 游戏窗口改尺寸后覆盖层与位置映射一起跟着变")
        else:
            print(f"  [FAIL] 改尺寸后未同步: rect={rect5} scale={scale5}")
            failures += 1

        browser.set_game_hwnd(0)
        time.sleep(1.5)
        fake_user32.DestroyWindow(fake_hwnd)
        restored = browser.mirror_debug(timeout=5.0) or {}
        if restored.get("game_hwnd"):
            print(f"  [SKIP] 本机真游戏在跑（hwnd={restored.get('game_hwnd')}），跳过回退断言")
        else:
            rect4 = list(((restored.get("overlay") or {}).get("rect") or []))
            window_rect4 = list(restored.get("window_rect") or [0, 0, 0, 0])
            if rect4 == window_rect4:
                print("  [OK] 游戏窗口不可用时覆盖层回退跟随悬浮窗（且缩放回到 1:1）")
            else:
                print(f"  [FAIL] 回退失败: {rect4} != {window_rect4}")
                failures += 1
    else:
        print("  [SKIP] 造不出假游戏窗口，跳过锚点断言")

    # 进程名校验：覆盖层所在的进程不是游戏进程，不该被误认成游戏窗口
    from src.gui.floating_browser import webview_process as wp

    overlay_hwnd = int(overlay.get("hwnd") or 0)
    exe = wp._window_process_exe(overlay_hwnd) if overlay_hwnd else ""
    if exe and exe not in wp.GAME_PROCESS_EXES:
        print(f"  [OK] 非游戏进程的窗口不会被当作游戏窗口（exe={exe}）")
    elif exe:
        print(f"  [FAIL] 测试进程被误判成游戏进程: {exe}")
        failures += 1
    else:
        print("  [SKIP] 读不到覆盖层进程名，跳过进程校验断言")

    # 覆盖层窗口的 Win32 属性
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    hwnd = user32.FindWindowW("OKWWDmakuOverlayCanvas", None)
    if not hwnd:
        print("  [FAIL] 按窗口类找不到覆盖层")
        failures += 1
    else:
        style = user32.GetWindowLongW(hwnd, -20)
        for mask, label in ((0x8, "置顶(WS_EX_TOPMOST)"),
                            (0x80000, "分层(WS_EX_LAYERED)"),
                            (0x20, "点击穿透(WS_EX_TRANSPARENT)"),
                            (0x80, "不出现在任务栏(WS_EX_TOOLWINDOW)")):
            ok = bool(style & mask)
            print(f"  {'[OK]' if ok else '[FAIL]'} 覆盖层窗口具备 {label}")
            if not ok:
                failures += 1

    # 热键动作切换
    browser.handle_action("toggle_mirror")
    time.sleep(1.2)
    info2 = browser.mirror_debug(timeout=5.0) or {}
    if info2.get("on") is False:
        print("  [OK] toggle_mirror 动作能关闭映射")
    else:
        print(f"  [FAIL] toggle_mirror 未生效: {info2.get('on')}")
        failures += 1

    # ---- 自绘模式：弹幕内容由子进程自己算，不再照抄页面 DOM 坐标 ----
    # 真实链路上弹幕是子进程按 cid 拉回来的（bilibili_danmaku），这里不联网，
    # 直接灌一段进引擎来验证「接线 + 渲染」；解析与引擎算法另有
    # tests/danmaku_engine_check.py 覆盖。
    print("\n  --- 自绘模式（自己拉数据自己算位置）---")
    browser.set_mirror_danmaku(True)
    time.sleep(1.0)
    demo = [
        {"t0": 0.0, "mode": 1, "size": 25, "color": 0xFFFFFF, "text": "自绘一号"},
        {"t0": 0.2, "mode": 1, "size": 25, "color": 0xFFD400, "text": "自绘二号"},
        {"t0": 0.4, "mode": 5, "size": 25, "color": 0x66CCFF, "text": "自绘顶部"},
        {"t0": 30.0, "mode": 1, "size": 25, "color": 0xFFFFFF, "text": "还没到时间"},
    ]
    browser.debug_load_danmaku(demo)
    browser.debug_set_playback(1.0, 1.0, False)
    time.sleep(1.5)
    auto = browser.mirror_debug(timeout=5.0) or {}
    aov = auto.get("overlay") or {}
    stats = aov.get("engine_stats") or {}
    for ok, label in [
        (auto.get("source") == "engine", f"数据源切到自绘（source={auto.get('source')}）"),
        (aov.get("engine_enabled") is True, "覆盖层处于自绘模式"),
        (int(stats.get("loaded") or 0) == len(demo), f"引擎载入 {stats.get('loaded')} 条"),
        (aov.get("last_draw", [0, 0, 0, False])[2] == 3,
         f"这一帧画 3 条（第 4 条还没到时间）-> {aov.get('last_draw')}"),
    ]:
        print(f"  {'[OK]' if ok else '[FAIL]'} {label}")
        if not ok:
            failures += 1
    texts = sorted(it["t"] for it in (aov.get("frame_items") or []))
    if texts:
        print(f"  [OK] 帧内容是引擎算出来的：{texts}")
    else:
        print("  [FAIL] 自绘模式下帧内容为空")
        failures += 1

    # 位置随播放时刻前移：把时钟固定在两个不同时刻各读一次
    # （用 paused=True 把时钟钉住，否则它会按 rate 自己往前走）
    browser.debug_set_playback(1.0, 1.0, True)
    time.sleep(1.2)
    at_first = ((browser.mirror_debug(timeout=5.0) or {}).get("overlay") or {})
    first_x = {it["t"]: it["x"] for it in (at_first.get("frame_items") or [])}
    browser.debug_set_playback(3.0, 1.0, True)
    time.sleep(1.2)
    at_third = ((browser.mirror_debug(timeout=5.0) or {}).get("overlay") or {})
    third_x = {it["t"]: it["x"] for it in (at_third.get("frame_items") or [])}
    moved = [k for k in first_x if k in third_x and third_x[k] < first_x[k]]
    if moved:
        print(f"  [OK] 位置随播放时刻前移（{len(moved)} 条，"
              f"t=1s 时 自绘一号 x={first_x.get('自绘一号')} -> t=3s 时 {third_x.get('自绘一号')}）")
    else:
        print(f"  [FAIL] 位置没有随时刻变化：{first_x} -> {third_x}")
        failures += 1

    # 暂停：再等一段时间，位置必须一点不动
    time.sleep(1.2)
    frozen = ((browser.mirror_debug(timeout=5.0) or {}).get("overlay") or {})
    frozen_x = {it["t"]: it["x"] for it in (frozen.get("frame_items") or [])}
    if (frozen.get("playback") or {}).get("paused") is True and frozen_x == third_x:
        print("  [OK] 暂停后位置冻住，画面不再前进")
    else:
        print(f"  [FAIL] 暂停没生效: 状态={(frozen.get('playback') or {}).get('paused')} "
              f"{third_x} -> {frozen_x}")
        failures += 1

    # 恢复播放：时钟按 rate 自己走，位置继续变化
    browser.debug_set_playback(3.0, 1.0, False)
    time.sleep(1.0)
    playing = ((browser.mirror_debug(timeout=5.0) or {}).get("overlay") or {})
    playing_x = {it["t"]: it["x"] for it in (playing.get("frame_items") or [])}
    if playing_x.get("自绘一号") is not None and playing_x != third_x:
        print(f"  [OK] 播放中时钟按倍速自走（坐标自行前进到 {playing_x.get('自绘一号')}）")
    else:
        print(f"  [FAIL] 播放中时钟没有推进: {third_x} -> {playing_x}")
        failures += 1

    # 拿不到弹幕时要能退回 DOM 模式（回退路径）
    browser.debug_load_danmaku([])
    time.sleep(1.2)
    fallback = browser.mirror_debug(timeout=5.0) or {}
    if fallback.get("source") == "dom" and (fallback.get("overlay") or {}).get("engine_enabled") is False:
        print("  [OK] 弹幕为空时自动退回 DOM 映射模式（回退路径可用）")
    else:
        print(f"  [FAIL] 回退失败: source={fallback.get('source')} "
              f"engine={ (fallback.get('overlay') or {}).get('engine_enabled') }")
        failures += 1

    # 「两个独立开关」走**悬浮窗工具条按钮**（ok 界面里已经没有这个开关了）：
    # 点弹幕按钮 -> 只开弹幕；再点字幕按钮 -> 两个都开；再点弹幕 -> 只剩字幕；
    # 再点字幕 -> 全关、覆盖层隐藏。
    buttons = browser.probe_js(
        "(function(){var d=document.getElementById('__ok_dm'),c=document.getElementById('__ok_cc');"
        "return (d?'dm':'')+(c?'cc':'');})()", timeout=5)
    if buttons == "dmcc":
        print("  [OK] 悬浮窗工具条上有「弹幕」「字幕」两个开关按钮")
    else:
        print(f"  [FAIL] 工具条开关按钮缺失: {buttons!r}")
        failures += 1

    def click_toolbar(button_id: str) -> None:
        browser.probe_js(f"document.getElementById('{button_id}').click(); true;", timeout=5)
        time.sleep(1.6)

    browser.set_mirror(False)
    time.sleep(1.2)
    click_toolbar("__ok_dm")
    only_dm = browser.mirror_debug(timeout=5.0) or {}
    ov_dm = only_dm.get("overlay") or {}
    dm_state_ok = bool(only_dm.get("danmaku")) and not only_dm.get("subtitle")
    print(f"  {'[OK]' if dm_state_ok else '[FAIL]'} 点弹幕按钮 -> 只开弹幕"
          f"（danmaku={only_dm.get('danmaku')} subtitle={only_dm.get('subtitle')}）")
    if not dm_state_ok:
        failures += 1
    dm_draw = (ov_dm.get("last_draw") or [0, 0, 0, False])
    if dm_draw[2] >= 1 and not dm_draw[3]:
        print("  [OK] 只开弹幕时只画弹幕、不画字幕")
    else:
        print(f"  [FAIL] 只开弹幕时绘制内容不对: dm={dm_draw[2]} sub={dm_draw[3]}")
        failures += 1

    click_toolbar("__ok_cc")
    both = browser.mirror_debug(timeout=5.0) or {}
    if both.get("danmaku") and both.get("subtitle"):
        print("  [OK] 再点字幕按钮 -> 弹幕 + 字幕都开（互不影响）")
    else:
        print(f"  [FAIL] 两个开关同时开失败: {both}")
        failures += 1

    click_toolbar("__ok_dm")
    only_sub = browser.mirror_debug(timeout=5.0) or {}
    ov_sub = only_sub.get("overlay") or {}
    sub_state_ok = (not only_sub.get("danmaku")) and bool(only_sub.get("subtitle"))
    print(f"  {'[OK]' if sub_state_ok else '[FAIL]'} 再点弹幕按钮 -> 只剩字幕"
          f"（danmaku={only_sub.get('danmaku')} subtitle={only_sub.get('subtitle')}）")
    if not sub_state_ok:
        failures += 1
    sub_draw = (ov_sub.get("last_draw") or [0, 0, 0, False])
    if sub_draw[2] == 0 and sub_draw[3]:
        print("  [OK] 只开字幕时只画字幕、不画弹幕")
    else:
        print(f"  [FAIL] 只开字幕时绘制内容不对: dm={sub_draw[2]} sub={sub_draw[3]}")
        failures += 1

    click_toolbar("__ok_cc")
    off = browser.mirror_debug(timeout=5.0) or {}
    if not off.get("on") and not off.get("danmaku") and not off.get("subtitle"):
        print("  [OK] 两个开关都关掉后覆盖层隐藏")
    else:
        print(f"  [FAIL] 全关后仍开着: {off}")
        failures += 1

    # 默认必须是关着的（子进程启动时两个开关都是 False）
    if not (off.get("danmaku") or off.get("subtitle")):
        print("  [OK] 关掉后状态为「都关」（默认不开启）")
    else:
        print("  [FAIL] 关掉后仍有开关处于开启状态")
        failures += 1

    browser.stop()
    try:
        if os.path.exists(MIRROR_HTML_PATH):
            os.remove(MIRROR_HTML_PATH)
    except OSError:
        pass
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="悬浮浏览器功能自测")
    parser.add_argument("--hotkeys", action="store_true", help="同时检查全局热键注册")
    parser.add_argument("--no-window", action="store_true", help="只做纯逻辑检查")
    parser.add_argument("--seconds", type=float, default=8.0, help="等待窗口就绪的秒数")
    args = parser.parse_args()

    failures = check_logic()
    failures += check_log_switch()
    if not args.no_window:
        failures += check_window(args.seconds)
        failures += check_danmaku_mirror(args.seconds)
    if args.hotkeys:
        failures += check_hotkeys()
        failures += check_hold_hook()

    try:
        if os.path.exists(HTML_PATH):
            os.remove(HTML_PATH)
        if os.path.exists(MIRROR_HTML_PATH):
            os.remove(MIRROR_HTML_PATH)
    except OSError:
        pass

    print(f"\n==== 失败项合计: {failures} ====")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
