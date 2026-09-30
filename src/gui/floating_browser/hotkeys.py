"""悬浮浏览器的全局快捷键管理。

使用 Win32 ``RegisterHotKey`` 注册系统级热键，因此在游戏处于前台、悬浮浏览器
失去焦点时依然有效。热键消息通过独立的 ``PeekMessage`` 轮询线程读取，
避免与 Qt 的主消息循环冲突。
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

from src.gui.floating_browser import log as fb_log


# ---------------------------------------------------------------------------
# 修饰键与虚拟键码
# ---------------------------------------------------------------------------
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
PM_NOREMOVE = 0x0000

# 低级键盘钩子（用于「按住 3 倍速」这类按下/松开交互）
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C


class KBDLLHOOKSTRUCT(ctypes.Structure):
    """低级键盘钩子回调的 lParam 指向的结构。"""

    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


# ---------------------------------------------------------------------------
# Win32 函数签名
# ---------------------------------------------------------------------------
# 这里刻意**不使用** ``ctypes.windll.user32``：``ok`` 框架已经把
# ``kernel32.GetModuleHandleW.restype`` 设成了 32 位的 ``c_long``，模块句柄会被
# 截断（真实值 140699985575936 → 1151926272）。把这个被截断的句柄传给
# ``SetWindowsHookExW`` 会让低级键盘钩子注册失败，表现为「按住3倍速完全无效」
# （异常发生在子线程里，主程序完全无感知）。
#
# 因此改用本模块私有的 ``ctypes.WinDLL`` 实例并显式声明签名：私有实例不会污染
# 全局的 ``ctypes.windll``，也就不会影响 ok 框架自己的设置。
HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p
)

if os.name == "nt":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.RegisterHotKey.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
    ]
    _user32.RegisterHotKey.restype = ctypes.c_int
    _user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.UnregisterHotKey.restype = ctypes.c_int

    _user32.PeekMessageW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
    ]
    _user32.PeekMessageW.restype = ctypes.c_int
    _user32.TranslateMessage.argtypes = [ctypes.c_void_p]
    _user32.TranslateMessage.restype = ctypes.c_int
    _user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t

    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short

    _user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, HOOKPROC, ctypes.c_void_p, ctypes.c_uint,
    ]
    _user32.SetWindowsHookExW.restype = ctypes.c_void_p
    _user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    _user32.UnhookWindowsHookEx.restype = ctypes.c_int
    _user32.CallNextHookEx.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _user32.CallNextHookEx.restype = ctypes.c_ssize_t

    _kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    _kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    _kernel32.GetCurrentThreadId.argtypes = []
    _kernel32.GetCurrentThreadId.restype = ctypes.c_uint
else:  # pragma: no cover - 非 Windows
    _user32 = None
    _kernel32 = None

_MODIFIER_ALIASES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "cmd": MOD_WIN,
    "meta": MOD_WIN,
}

# 主键名 -> 虚拟键码
_KEY_CODES: dict[str, int] = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "minus": 0xBD,
    "-": 0xBD,
    "equal": 0xBB,
    "=": 0xBB,
    ",": 0xBC,
    "comma": 0xBC,
    ".": 0xBE,
    "period": 0xBE,
    "/": 0xBF,
    "slash": 0xBF,
    "`": 0xC0,
    "backquote": 0xC0,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
}
for _index in range(1, 25):
    _KEY_CODES[f"f{_index}"] = 0x6F + _index  # F1 = 0x70
for _char in "abcdefghijklmnopqrstuvwxyz":
    _KEY_CODES[_char] = ord(_char.upper())
for _digit in "0123456789":
    _KEY_CODES[_digit] = ord(_digit)

# 媒体键：不依赖修饰键，适合做播放控制
MEDIA_KEYS = {
    "play_pause": 0xB3,  # VK_MEDIA_PLAY_PAUSE
    "next": 0xB0,        # VK_MEDIA_NEXT_TRACK
    "prev": 0xB1,        # VK_MEDIA_PREV_TRACK
    "stop": 0xB2,        # VK_MEDIA_STOP
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "volume_mute": 0xAD,
}

# ---------------------------------------------------------------------------
# 预设动作定义：供配置界面生成选项
# ---------------------------------------------------------------------------
# 默认组合刻意避开 Ctrl+Alt+方向键 —— 该组合在 Intel / AMD 显卡驱动上被
# 用作屏幕旋转快捷键，注册必然失败。这里统一改用 Ctrl+Shift+方向键。
HOTKEY_ACTIONS: dict[str, dict] = {
    "play_pause": {
        "label": "Play / Pause",
        "description": "Toggle video playback",
        "default": "alt+s",
        "step": None,
    },
    "forward": {
        "label": "Forward",
        "description": "Skip forward by the configured seconds",
        "default": "alt+d",
        "step": 5.0,
    },
    "backward": {
        "label": "Backward",
        "description": "Skip backward by the configured seconds",
        "default": "alt+a",
        "step": 5.0,
    },
    "hold_fast_forward": {
        "label": "Hold for 3x speed",
        "description": "Play at 3x while held, restore the original speed on release",
        "default": "alt+f",
        "step": 3.0,
    },
    "volume_up": {
        "label": "Volume +10%",
        "description": "Raise the volume by 10%",
        "default": "alt+c",
        "step": 0.1,
    },
    "volume_down": {
        "label": "Volume -10%",
        "description": "Lower the volume by 10%",
        "default": "alt+x",
        "step": 0.1,
    },
    "toggle_click_through": {
        "label": "Toggle click-through",
        "description": "Turn mouse click-through on or off",
        "default": "`",
        "step": None,
    },
    "toggle_mirror": {
        "label": "Toggle overlay mirroring",
        "description": "Mirror Bilibili danmaku/subtitles onto the game; press again to stop",
        "default": "alt+m",
        "step": None,
    },
}


def parse_hotkey(text: str) -> Optional[tuple[int, int]]:
    """把 ``ctrl+alt+right`` 这类字符串解析为 ``(modifiers, vk)``。

    解析失败返回 ``None``。
    """
    if not text or not isinstance(text, str):
        return None
    text = text.strip().lower()
    if not text or text in {"none", "disabled", "off"}:
        return None

    parts = [part.strip() for part in text.replace(" ", "").split("+") if part.strip()]
    if not parts:
        return None

    modifiers = 0
    key_code: Optional[int] = None
    for part in parts:
        if part in _MODIFIER_ALIASES:
            modifiers |= _MODIFIER_ALIASES[part]
            continue
        code = _KEY_CODES.get(part)
        if code is None and len(part) == 1 and part.isalnum():
            code = ord(part.upper())
        if code is None:
            return None
        if key_code is not None:
            # 出现了两个主键，视为非法组合
            return None
        key_code = code

    if key_code is None:
        return None
    return modifiers, key_code


def pretty_hotkey(text: str) -> str:
    """把热键字符串格式化为更适合展示的形式。"""
    if not text:
        return "Not set"
    return " + ".join(part.strip().upper() if len(part.strip()) == 1 else part.strip().capitalize()
                      for part in text.split("+"))


class HotkeyManager:
    """全局热键管理器。

    参数
    ----
    dispatch: 动作分发回调，签名为 ``dispatch(action: str, value: float | None)``。
    hwnd: 可选，注册热键时绑定的窗口句柄；``None`` 表示进程级热键。
    """

    def __init__(
        self,
        dispatch: Callable[[str, Optional[float]], None],
        hwnd: Optional[int] = None,
    ) -> None:
        self._dispatch = dispatch
        self._hwnd = hwnd
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._registered: dict[int, tuple[str, Optional[float]]] = {}
        self._lock = threading.RLock()
        self._next_id = 0xC000  # 避开框架已使用的低位 id
        self._failed: list[str] = []
        # 注册/注销命令队列：RegisterHotKey 必须在消息循环线程里调用，
        # 否则 WM_HOTKEY 会投递到「调用线程」的消息队列，轮询线程永远收不到。
        self._cmd_queue: queue.Queue = queue.Queue()

    @property
    def failed_bindings(self) -> list[str]:
        """注册失败的热键列表（通常是被其它软件占用）。"""
        return list(self._failed)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------
    def start(self) -> None:
        if os.name != "nt":
            fb_log.info("非 Windows 平台，跳过全局热键注册")
            return
        with self._lock:
            if self.running:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._message_loop, name="FloatingBrowserHotkey", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        # 唤醒消息循环线程，让它尽快退出并在退出前注销全部热键
        self._cmd_queue.put(("__wake__",))
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._lock:
            self._registered.clear()

    # ------------------------------------------------------------------
    # 绑定管理
    # ------------------------------------------------------------------
    def bind(self, action: str, hotkey: str, value: Optional[float] = None) -> bool:
        """为某个动作绑定热键，返回是否成功。"""
        if os.name != "nt":
            return False
        parsed = parse_hotkey(hotkey)
        if parsed is None:
            if hotkey:
                fb_log.info(f"热键 {hotkey} 无效或已禁用，跳过 {action}")
            return False
        modifiers, key_code = parsed
        with self._lock:
            hotkey_id = self._next_id
            self._next_id += 1
        # RegisterHotKey 必须在消息循环线程里执行（WM_HOTKEY 投递到调用线程队列）
        result_queue: queue.Queue = queue.Queue()
        self._cmd_queue.put(
            ("register", hotkey_id, modifiers, key_code, action, value, result_queue)
        )
        try:
            ok = bool(result_queue.get(timeout=3.0))
        except queue.Empty:
            ok = False
        if not ok:
            fb_log.info(f"注册热键失败（可能被占用）: {action} -> {hotkey}")
            self._failed.append(f"{action} ({hotkey})")
            return False
        fb_log.info(f"注册热键成功: {action} -> {hotkey}")
        return True

    def bind_all(self, bindings: dict[str, dict]) -> dict[str, bool]:
        """批量绑定。

        ``bindings`` 形如 ``{"play_pause": {"hotkey": "ctrl+alt+space", "value": None}, ...}``
        """
        results: dict[str, bool] = {}
        with self._lock:
            self._failed.clear()
        for action, config in (bindings or {}).items():
            if not isinstance(config, dict):
                results[action] = False
                continue
            results[action] = self.bind(
                action, config.get("hotkey", ""), config.get("value")
            )
        return results

    # ------------------------------------------------------------------
    # 消息循环
    # ------------------------------------------------------------------
    def _message_loop(self) -> None:
        msg = wintypes.MSG()
        user32 = _user32
        # 记录本线程的 Windows 线程 id，便于诊断与测试（PostThreadMessage 用）
        self._win_thread_id = int(_kernel32.GetCurrentThreadId())
        # 先 PeekMessage 一次，确保为本线程创建消息队列；此后在本线程里
        # RegisterHotKey(NULL, ...) 产生的 WM_HOTKEY 才会进这个队列。
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        while not self._stop_event.is_set():
            # 1) 处理主线程发来的注册/注销命令
            while True:
                try:
                    command = self._cmd_queue.get_nowait()
                except queue.Empty:
                    break
                self._handle_command(command, user32)

            # 2) 处理 WM_HOTKEY（使用 PeekMessage 抽干队列，避免阻塞无法响应停止信号）
            handled = False
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                handled = True
                if msg.message == WM_HOTKEY:
                    self._on_hotkey(int(msg.wParam))
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if not handled:
                self._stop_event.wait(0.02)

        # 退出前在本线程注销全部热键，保持干净
        with self._lock:
            for hotkey_id in list(self._registered):
                try:
                    user32.UnregisterHotKey(self._hwnd, hotkey_id)
                except Exception as error:
                    fb_log.debug(f"注销热键 {hotkey_id} 失败: {error}")
            self._registered.clear()
        # 线程退出前翻译剩余消息
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _handle_command(self, command, user32) -> None:
        """在消息循环线程里处理注册 / 注销命令。"""
        kind = command[0]
        if kind == "register":
            _, hotkey_id, modifiers, key_code, action, value, result_queue = command
            try:
                ok = bool(
                    user32.RegisterHotKey(
                        self._hwnd, hotkey_id, modifiers | MOD_NOREPEAT, key_code
                    )
                )
            except Exception as error:
                fb_log.debug(f"RegisterHotKey 异常: {error}")
                ok = False
            if ok:
                with self._lock:
                    self._registered[hotkey_id] = (action, value)
            result_queue.put(ok)
        elif kind == "unregister":
            _, hotkey_id = command
            try:
                user32.UnregisterHotKey(self._hwnd, hotkey_id)
            except Exception as error:
                fb_log.debug(f"注销热键 {hotkey_id} 失败: {error}")
            with self._lock:
                self._registered.pop(hotkey_id, None)

    def _on_hotkey(self, hotkey_id: int) -> None:
        with self._lock:
            entry = self._registered.get(hotkey_id)
        if entry is None:
            return
        action, value = entry
        try:
            self._dispatch(action, value)
        except Exception as error:
            fb_log.error(f"处理热键动作 {action} 失败: {error}")


class HoldKeyManager:
    """「按住」类热键：区分目标键的按下 / 松开，用于「按住 3 倍速」。

    ``RegisterHotKey`` 只会在按键按下时触发一次，无法表达「松开恢复」；
    这里改用低级键盘钩子（``WH_KEYBOARD_LL``），能拿到全局的按下与松开两个
    事件（即使游戏在前台也有效）。
    """

    def __init__(
        self,
        on_down: Callable[[], None],
        on_up: Callable[[], None],
    ) -> None:
        self._on_down = on_down
        self._on_up = on_up
        self._thread: Optional[threading.Thread] = None
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._hook = None
        self._hook_proc = None  # 必须保持引用，否则回调被 GC 后钩子会失效
        self._target_vk: Optional[int] = None
        self._target_modifiers = 0
        self._held = False
        self._installed = False
        self._hook_error = ""
        self._lock = threading.RLock()
        # 钩子回调必须**尽快返回**（Windows 默认 300ms 超时后会静默卸载钩子），
        # 所以回调里只做入队，真正的动作（写子进程 stdin 等）交给工作线程。
        self._event_queue: queue.Queue = queue.Queue()

    def set_hotkey(self, hotkey_text: str) -> bool:
        parsed = parse_hotkey(hotkey_text)
        if parsed is None:
            self._target_vk = None
            self._target_modifiers = 0
            return False
        self._target_modifiers, self._target_vk = parsed
        return True

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def installed(self) -> bool:
        """低级键盘钩子是否真的安装成功（False 表示「按住」功能不可用）。"""
        return bool(self._installed)

    @property
    def last_error(self) -> str:
        """安装失败时的原因，便于在界面上提示用户。"""
        return self._hook_error

    def start(self) -> None:
        if os.name != "nt" or self._target_vk is None:
            return
        with self._lock:
            if self.running:
                return
            self._stop_event.clear()
            self._held = False
            self._installed = False
            self._hook_error = ""
            self._worker = threading.Thread(
                target=self._worker_loop, name="FloatingBrowserHoldKeyJobs", daemon=True
            )
            self._worker.start()
            self._thread = threading.Thread(
                target=self._message_loop, name="FloatingBrowserHoldKey", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._event_queue.put(("__wake__", None))
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._worker is not None:
            self._worker.join(timeout=1.0)
            self._worker = None
        with self._lock:
            self._held = False
            self._installed = False

    # ------------------------------------------------------------------
    # 动作线程（避免在钩子回调里做耗时操作）
    # ------------------------------------------------------------------
    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                kind, _ = self._event_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind == "__wake__":
                continue
            handler = self._on_down if kind == "down" else self._on_up
            try:
                handler()
            except Exception as error:
                fb_log.error(f"按住键动作 {kind} 执行失败: {error}")

    # ------------------------------------------------------------------
    # 修饰键状态
    # ------------------------------------------------------------------
    def _modifiers_down(self, user32) -> bool:
        def down(vk: int) -> bool:
            return (user32.GetAsyncKeyState(vk) & 0x8000) != 0

        mods = self._target_modifiers
        if mods & MOD_CONTROL and not down(VK_CONTROL):
            return False
        if mods & MOD_SHIFT and not down(VK_SHIFT):
            return False
        if mods & MOD_ALT and not down(VK_MENU):
            return False
        if mods & MOD_WIN and not (down(VK_LWIN) or down(VK_RWIN)):
            return False
        return True

    def _install_hook(self, user32, kernel32):
        """安装低级键盘钩子，返回 (hook, error_text)。"""
        hook_holder = {"hook": None}
        hook_proc = HOOKPROC(self._make_callback(user32, hook_holder))
        self._hook_proc = hook_proc  # 保持引用防 GC

        # hMod 必须是完整的 64 位模块句柄；ok 框架把 GetModuleHandleW.restype
        # 设成 c_long 会让句柄被截断、导致安装失败，所以这里用私有 _kernel32。
        candidates = []
        try:
            hmod = kernel32.GetModuleHandleW(None)
            if hmod:
                candidates.append(hmod)
        except Exception:
            pass
        candidates.append(None)  # 兜底：低级钩子允许 hMod = NULL

        last_error = ""
        for hmod in candidates:
            ctypes.set_last_error(0)
            hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook_proc, hmod, 0)
            if hook:
                hook_holder["hook"] = hook
                return hook, ""
            last_error = f"SetWindowsHookExW 失败, hMod={hmod}, err={ctypes.get_last_error()}"
        return None, last_error

    def _make_callback(self, user32, hook_holder):
        def callback(nCode, wParam, lParam):
            if nCode == HC_ACTION:
                try:
                    kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    vk = int(kb.vkCode)
                    if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        if vk == self._target_vk and self._modifiers_down(user32):
                            if not self._held:
                                self._held = True
                                self._event_queue.put(("down", None))
                    elif wParam in (WM_KEYUP, WM_SYSKEYUP):
                        if vk == self._target_vk:
                            if self._held:
                                self._held = False
                                self._event_queue.put(("up", None))
                except Exception as error:
                    fb_log.debug(f"低级键盘钩子回调异常: {error}")
            return user32.CallNextHookEx(hook_holder["hook"], nCode, wParam, lParam)

        return callback

    def _message_loop(self) -> None:
        user32, kernel32 = _user32, _kernel32
        hook, error_text = self._install_hook(user32, kernel32)
        if not hook:
            self._hook_error = error_text or "未知原因"
            fb_log.error(
                f"低级键盘钩子注册失败，按住3倍速将不可用（{self._hook_error}）"
            )
            return
        self._hook = hook
        self._installed = True
        fb_log.info("低级键盘钩子已安装（按住3倍速可用）")

        msg = wintypes.MSG()
        while not self._stop_event.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                self._stop_event.wait(0.02)

        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        self._installed = False
        self._hook_proc = None
