"""悬浮浏览器服务层。

把 ``FloatingBrowser``（窗口 + 视频控制）与 ``HotkeyManager``（全局热键）
组合成一个可被界面和任务共同使用的单例服务，统一处理：

- 读取全局配置（地址、尺寸、透明度、热键）
- 启动 / 停止悬浮浏览器
- 热键动作分发到视频控制
- 状态变更通知界面刷新
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional


from src.gui.floating_browser.browser import FloatingBrowser, BrowserState, normalize_url
from src.gui.floating_browser.hotkeys import HOTKEY_ACTIONS, HoldKeyManager, HotkeyManager
from src.gui.floating_browser import log as fb_log


FLOATING_BROWSER_CONFIG = "Floating Browser"
HOTKEY_CONFIG = "Floating Browser Hotkey"


class FloatingBrowserService:
    """悬浮浏览器的全局单例服务。"""

    _instance: Optional["FloatingBrowserService"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._browser: Optional[FloatingBrowser] = None
        self._hotkeys: Optional[HotkeyManager] = None
        self._hold: Optional[HoldKeyManager] = None
        self._lock = threading.RLock()
        self._listeners: list[Callable[[BrowserState], None]] = []
        self._geometry_listeners: list[Callable[[tuple], None]] = []
        self._ui_listeners: list[Callable[[dict], None]] = []
        self._status_listeners: list[Callable[[str], None]] = []
        self._last_message = ""
        self._log_callback: Optional[Callable[[str], None]] = None
        self._failed_hotkeys: list[str] = []
        # 最近一次推给子进程的游戏窗口句柄（弹幕覆盖层的锚点）
        self._game_hwnd = 0

    @property
    def failed_hotkeys(self) -> list[str]:
        """注册失败（通常被其它程序占用）的热键列表。"""
        return list(self._failed_hotkeys)

    # ------------------------------------------------------------------
    # 单例
    # ------------------------------------------------------------------
    @classmethod
    def instance(cls) -> "FloatingBrowserService":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = FloatingBrowserService()
            return cls._instance

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def browser(self) -> Optional[FloatingBrowser]:
        return self._browser

    @property
    def running(self) -> bool:
        return self._browser is not None and self._browser.running

    @property
    def last_message(self) -> str:
        return self._last_message

    def add_state_listener(self, listener: Callable[[BrowserState], None]) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)
        if self._browser is not None:
            self._browser.add_state_listener(listener)

    def add_status_listener(self, listener: Callable[[str], None]) -> None:
        with self._lock:
            if listener not in self._status_listeners:
                self._status_listeners.append(listener)

    def add_geometry_listener(self, listener: Callable[[tuple], None]) -> None:
        """监听悬浮窗几何变化（用户拖动 / 缩放）。"""
        with self._lock:
            if listener not in self._geometry_listeners:
                self._geometry_listeners.append(listener)
        if self._browser is not None:
            self._browser.add_geometry_listener(listener)

    def add_ui_listener(self, listener: Callable[[dict], None]) -> None:
        """监听悬浮工具条事件。"""
        with self._lock:
            if listener not in self._ui_listeners:
                self._ui_listeners.append(listener)
        if self._browser is not None:
            self._browser.add_ui_listener(listener)

    def drain_ui_events(self) -> list[dict]:
        if self._browser is None:
            return []
        return self._browser.drain_ui_events()

    def set_log_callback(self, callback: Optional[Callable[[str], None]]) -> None:
        """设置日志回调，让悬浮浏览器日志写进应用日志。"""
        self._log_callback = callback

    def set_verbose(self, enabled: bool) -> None:
        """悬浮浏览器日志总开关（配置页勾选框，改完即时生效）。"""
        fb_log.set_verbose(enabled)

    def _log(self, message: str) -> None:
        fb_log.info(message)
        self._last_message = message
        if self._log_callback is not None:
            try:
                self._log_callback(message)
            except Exception:
                pass
        for listener in list(self._status_listeners):
            try:
                listener(message)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    def _read_config(self) -> dict:
        """从全局配置读取悬浮浏览器参数，读取失败时返回默认值。"""
        defaults = {
            "url": "https://www.bilibili.com/",
            "width": 560,
            "height": 340,
            "opacity": 0.9,
            "on_top": True,
            "click_through": False,
            "seek_step": 5,
            "hover_opacity": 0.3,
            "log_output": False,
            "mirror_danmaku": False,
            "mirror_subtitle": False,
        }
        try:
            from ok import og

            config = og.executor.global_config.get_config(FLOATING_BROWSER_CONFIG)
            result = dict(defaults)
            for key in defaults:
                if key in config:
                    result[key] = config[key]
            return result
        except Exception as error:
            fb_log.debug(f"读取悬浮浏览器配置失败，使用默认值: {error}")
            return defaults

    def _detect_game_hwnd(self) -> int:
        """读取 ok 框架已解析好的游戏窗口句柄（取不到返回 0）。

        弹幕/字幕覆盖层要贴在**游戏画面**上，所以需要游戏窗口句柄。ok 的
        ``device_manager`` 里已经有（``config.py`` 的 ``windows.hwnd_class``）；
        取不到时子进程还会自己去按类名+进程名搜，所以这里失败不影响功能。
        """
        try:
            from ok import og

            manager = getattr(og, "device_manager", None)
            window = getattr(manager, "hwnd_window", None)
            if window is None:
                capture = getattr(manager, "capture_method", None)
                window = getattr(capture, "hwnd_window", None)
            return int(getattr(window, "hwnd", 0) or 0)
        except Exception as error:
            fb_log.debug(f"读取游戏窗口句柄失败: {error}")
            return 0

    def sync_game_hwnd(self) -> None:
        """把最新的游戏窗口句柄同步给子进程（游戏重启后句柄会变）。"""
        hwnd = self._detect_game_hwnd()
        if hwnd == self._game_hwnd:
            return
        self._game_hwnd = hwnd
        if self._browser is not None:
            self._browser.set_game_hwnd(hwnd)

    def _read_hotkeys(self) -> dict[str, dict]:
        """读取热键配置，返回 ``{action: {"hotkey": ..., "value": ...}}``。"""
        bindings: dict[str, dict] = {}
        config: dict = {}
        try:
            from ok import og

            config = og.executor.global_config.get_config(HOTKEY_CONFIG)
        except Exception as error:
            fb_log.debug(f"读取悬浮浏览器热键配置失败，使用默认值: {error}")

        for action, meta in HOTKEY_ACTIONS.items():
            hotkey = config.get(action, meta["default"]) if config else meta["default"]
            bindings[action] = {"hotkey": hotkey, "value": meta.get("step")}
        return bindings

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------
    def start(self, url: Optional[str] = None) -> bool:
        """启动悬浮浏览器，返回是否进入 WebView2 模式。"""
        # 日志总开关：每次启动都按当前配置刷新一次，子进程也会拿到同一个值
        fb_log.set_verbose(bool(self._read_config().get("log_output", False)))
        with self._lock:
            if self.running:
                if url:
                    self._browser.load_url(normalize_url(url))
                self._sync_opacity_from_config()
                self.sync_game_hwnd()
                self._log("悬浮浏览器已在运行")
                return not self._browser.degraded

            config = self._read_config()
            target = normalize_url(url or config.get("url", ""))
            # 弹幕/字幕覆盖层的锚点：游戏窗口（取不到就跟随悬浮窗）
            self._game_hwnd = self._detect_game_hwnd()
            browser = FloatingBrowser(
                url=target,
                width=int(config.get("width", 560)),
                height=int(config.get("height", 340)),
                opacity=float(config.get("opacity", 0.9)),
                on_top=bool(config.get("on_top", True)),
                click_through=bool(config.get("click_through", False)),
                mirror_danmaku=bool(config.get("mirror_danmaku", False)),
                mirror_subtitle=bool(config.get("mirror_subtitle", False)),
                game_hwnd=self._game_hwnd,
                hover_opacity=float(config.get("hover_opacity", 0.3)),
                on_closed=self._on_browser_closed,
                log_info=self._log,
            )
            for listener in list(self._listeners):
                browser.add_state_listener(listener)
            for listener in list(self._geometry_listeners):
                browser.add_geometry_listener(listener)
            for listener in list(self._ui_listeners):
                browser.add_ui_listener(listener)
            self._browser = browser

        webview_mode = browser.start()

        hotkeys = HotkeyManager(dispatch=self._dispatch_action)
        hotkeys.start()
        bindings = self._read_hotkeys()
        # 「按住3倍速」是按下/松开交互，用低级键盘钩子单独处理，不走 RegisterHotKey
        hold_binding = bindings.pop("hold_fast_forward", None)
        results = hotkeys.bind_all(bindings)
        with self._lock:
            self._hotkeys = hotkeys
        self._start_hold(hold_binding)

        ok_count = sum(1 for success in results.values() if success)
        if ok_count:
            self._log(f"已注册 {ok_count} 个全局热键（可在配置页修改）")
        with self._lock:
            self._failed_hotkeys = list(hotkeys.failed_bindings)
        if hotkeys.failed_bindings:
            self._log(
                "以下热键被其它程序占用，注册失败: "
                + ", ".join(hotkeys.failed_bindings)
                + "。常见占用方：微信/QQ/WeGame/网易 GameViewer、游戏覆盖层与录屏工具"
                "（如 AMD 录屏、网易云音乐）、游戏语音（如 Oopz）。"
                "可在配置页改用其它组合（本机实测 ctrl+shift+字母 全部空闲）。"
            )

        if not webview_mode:
            self._log("未启用内嵌浏览器，已用外部浏览器打开视频页面")

        # 应用置顶状态
        if not config.get("on_top", True):
            browser.set_on_top(False)
        return webview_mode

    def stop(self) -> None:
        """停止悬浮浏览器并注销热键。"""
        with self._lock:
            hotkeys = self._hotkeys
            hold = self._hold
            browser = self._browser
            self._hotkeys = None
            self._hold = None
            self._browser = None
        if hold is not None:
            hold.stop()
        if hotkeys is not None:
            hotkeys.stop()
        if browser is not None:
            browser.stop()
        self._log("悬浮浏览器已停止")

    def _on_browser_closed(self) -> None:
        with self._lock:
            if self._hotkeys is not None:
                self._hotkeys.stop()
                self._hotkeys = None
            self._browser = None
        self._log("悬浮浏览器窗口已关闭")

    def _sync_opacity_from_config(self) -> None:
        config = self._read_config()
        if self._browser is not None:
            self._browser.set_opacity(float(config.get("opacity", 0.9)))

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    def _dispatch_action(self, action: str, value: Optional[float]) -> None:
        browser = self._browser
        if browser is None:
            return
        if value is None:
            value = self._read_config().get("seek_step", 5)
        browser.handle_action(action, value)

    # ------------------------------------------------------------------
    # 按住 3 倍速
    # ------------------------------------------------------------------
    def _start_hold(self, binding: Optional[dict]) -> None:
        """启动「按住3倍速」低级键盘钩子。"""
        if self._hold is not None:
            self._hold.stop()
            self._hold = None
        if not binding or not binding.get("hotkey"):
            return
        hold = HoldKeyManager(
            on_down=self._on_hold_down,
            on_up=self._on_hold_up,
        )
        if not hold.set_hotkey(binding["hotkey"]):
            if binding.get("hotkey"):
                self._log(f"「按住3倍速」热键 {binding['hotkey']} 无效，已跳过")
            return
        hold.start()
        with self._lock:
            self._hold = hold
        # 钩子是在子线程里安装的，等一小会儿确认结果，失败要明确告诉用户
        # （否则「按住没反应」会被误判成播放器不兼容）。
        for _ in range(20):
            if hold.installed:
                self._log(
                    f"「按住3倍速」已启用（{binding['hotkey']}，按住 3 倍速、松开恢复）"
                )
                return
            if not hold.running:
                break
            time.sleep(0.05)
        if not hold.installed:
            self._log(
                "「按住3倍速」未能启用：低级键盘钩子安装失败"
                + (f"（{hold.last_error}）" if hold.last_error else "")
            )

    def _on_hold_down(self) -> None:
        """按住目标键：切到 3 倍速（原倍速由页面脚本记录）。"""
        browser = self._browser
        if browser is None:
            return
        browser.set_hold_rate(3.0)

    def _on_hold_up(self) -> None:
        """松开目标键：恢复按住之前的倍速。"""
        browser = self._browser
        if browser is None:
            return
        browser.release_hold_rate()

    def apply_action(self, action: str, value: Optional[float] = None) -> BrowserState:
        """供界面按钮调用，直接对视频执行动作。"""
        browser = self._browser
        if browser is None:
            self._log("悬浮浏览器尚未启动")
            return BrowserState()
        if value is None and action in {"forward", "backward"}:
            value = self._read_config().get("seek_step", 5)
        return browser.handle_action(action, value)

    # ------------------------------------------------------------------
    # 界面即时调整
    # ------------------------------------------------------------------
    def apply_opacity(self, opacity: float) -> None:
        """透明度仅由悬浮窗自带工具条或配置文件调整。"""
        if self._browser is not None:
            self._browser.set_opacity(opacity)

    def set_click_through(self, enabled: bool) -> None:
        if self._browser is not None:
            self._browser.set_click_through(enabled)

    def set_hover_opacity(self, opacity: float) -> None:
        if self._browser is not None:
            self._browser.set_hover_opacity(opacity)

    def set_mirror(self, enabled: bool) -> bool:
        """总开关（工具条按钮 / 热键用）：同时开/关弹幕与字幕。"""
        if self._browser is None:
            self._log("悬浮浏览器尚未启动，无法开启弹幕映射")
            return False
        if enabled:
            # 开启前先把游戏窗口句柄同步过去，覆盖层才能一次就锚到正确位置
            self.sync_game_hwnd()
        return self._browser.set_mirror(enabled)

    def set_mirror_danmaku(self, enabled: bool) -> bool:
        """单独开关「弹幕」映射。"""
        if self._browser is None:
            self._log("悬浮浏览器尚未启动，无法开启弹幕映射")
            return False
        if enabled:
            self.sync_game_hwnd()
        return self._browser.set_mirror_danmaku(enabled)

    def set_mirror_subtitle(self, enabled: bool) -> bool:
        """单独开关「字幕」映射。"""
        if self._browser is None:
            self._log("悬浮浏览器尚未启动，无法开启字幕映射")
            return False
        if enabled:
            self.sync_game_hwnd()
        return self._browser.set_mirror_subtitle(enabled)

    @property
    def mirror_enabled(self) -> bool:
        return bool(self._browser is not None and self._browser.mirror_enabled)

    @property
    def mirror_danmaku_enabled(self) -> bool:
        return bool(self._browser is not None and self._browser.mirror_danmaku_enabled)

    @property
    def mirror_subtitle_enabled(self) -> bool:
        return bool(self._browser is not None and self._browser.mirror_subtitle_enabled)

    @property
    def game_hwnd(self) -> int:
        """最近一次推给子进程的游戏窗口句柄（0 = 没找到，覆盖层会跟随悬浮窗）。"""
        return self._game_hwnd

    def geometry(self) -> tuple[int, int, int, int]:
        if self._browser is None:
            return (-1, -1, 0, 0)
        return self._browser.geometry

    def saved_size(self) -> tuple[int, int]:
        """读取上次保存的窗口尺寸（用于界面展示）。"""
        config = self._read_config()
        return int(config.get("width", 560)), int(config.get("height", 340))

    def open_url(self, url: str) -> None:
        if self._browser is not None:
            self._browser.load_url(normalize_url(url))

    def toggle_visible(self) -> None:
        if self._browser is not None:
            self._browser.toggle_visible()

    def refresh_state(self) -> BrowserState:
        if self._browser is None:
            return BrowserState()
        # 顺手同步游戏窗口句柄（游戏重启后句柄会变，覆盖层要跟着换锚点）
        self.sync_game_hwnd()
        return self._browser.refresh_state()
