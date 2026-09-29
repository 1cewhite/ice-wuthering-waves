"""悬浮浏览器核心窗口（主进程侧）。

实现思路
--------
``pywebview.start()`` 阻塞且必须在主线程运行，无法与 Qt 事件循环共存。
因此把真正的 WebView2 窗口放进**独立子进程**（``webview_process``），
主进程保留：

- 通过子进程的 stdin/stdout 管道下发指令（改尺寸、透明度、播放控制等）；
- 读取状态队列，缓存最近的视频状态供界面与快捷键使用；
- 用 Win32 句柄不可直接访问，因此透明度/置顶等由子进程内的 Win32 调用完成。

当系统缺少 WebView2 运行时或未安装 ``pywebview`` 时，自动降级为
“用系统浏览器打开视频页面”，并明确告知用户。
"""

from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any, Callable, Optional
from urllib.parse import quote

from src.gui.floating_browser import log as fb_log


# 子进程协议行前缀，需与 _launcher.PROTOCOL_PREFIX 保持一致
PROTOCOL_PREFIX = "@@OKFB@@"

# ---------------------------------------------------------------------------
# 子进程生命周期管理：确保应用退出时不留残余进程
# ---------------------------------------------------------------------------
_LIVE_PROCESSES: set = set()
_LIVE_LOCK = threading.Lock()


def _kill_process_tree(pid: int) -> None:
    """结束指定进程及其全部子进程（WebView2 会派生子进程）。

    使用 ``taskkill /T`` 递归结束，避免残留 msedgewebview2.exe 进程。
    """
    if os.name != "nt" or not pid:
        return
    try:
        import subprocess

        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as error:
        fb_log.debug(f"结束进程树 {pid} 失败: {error}")


def _cleanup_processes() -> None:
    with _LIVE_LOCK:
        processes = list(_LIVE_PROCESSES)
        _LIVE_PROCESSES.clear()
    for process in processes:
        try:
            if process.poll() is None:
                _kill_process_tree(process.pid)
                try:
                    process.wait(timeout=2)
                except Exception:
                    pass
        except Exception:
            pass


atexit.register(_cleanup_processes)


@dataclass
class BrowserState:
    """视频当前状态的快照。"""

    found: bool = False
    paused: bool = True
    current: float = 0.0
    duration: float = 0.0
    rate: float = 1.0
    volume: float = 1.0
    muted: bool = False
    title: str = ""

    @classmethod
    def from_payload(cls, payload: Any) -> "BrowserState":
        if not isinstance(payload, dict):
            return cls()
        def _num(key: str, default: float) -> float:
            try:
                return float(payload.get(key) if payload.get(key) is not None else default)
            except (TypeError, ValueError):
                return default

        return cls(
            found=bool(payload.get("found")),
            paused=bool(payload.get("paused", True)),
            current=_num("current", 0.0),
            duration=_num("duration", 0.0),
            rate=_num("rate", 1.0),
            volume=_num("volume", 1.0),
            muted=bool(payload.get("muted")),
            title=str(payload.get("title") or ""),
        )


def _format_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class FloatingBrowser:
    """悬浮浏览器窗口（主进程代理对象）。"""

    DEFAULT_SIZE = (560, 340)
    MIN_SIZE = (240, 140)
    MAX_SIZE = (3840, 2160)

    def __init__(
        self,
        url: str = "https://www.bilibili.com/",
        width: int = 560,
        height: int = 340,
        opacity: float = 0.9,
        x: Optional[int] = None,
        y: Optional[int] = None,
        on_top: bool = True,
        click_through: bool = False,
        mirror_danmaku: bool = False,
        mirror_subtitle: bool = False,
        game_hwnd: int = 0,
        hover_opacity: float = 0.3,
        on_closed: Optional[Callable[[], None]] = None,
        log_info: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._url = url or "about:blank"
        self._width = int(self._clamp(width, self.MIN_SIZE[0], self.MAX_SIZE[0]))
        self._height = int(self._clamp(height, self.MIN_SIZE[1], self.MAX_SIZE[1]))
        self._opacity = self._clamp(opacity, 0.2, 1.0)
        self._hover_opacity = self._clamp(hover_opacity, 0.05, 1.0)
        self._x = x
        self._y = y
        self._on_top = bool(on_top)
        self._click_through = bool(click_through)
        # 弹幕 / 字幕两个独立开关；``_mirror`` 是「至少开一个」的总状态
        self._mirror_danmaku = bool(mirror_danmaku)
        self._mirror_subtitle = bool(mirror_subtitle)
        self._mirror = bool(mirror_danmaku or mirror_subtitle)
        # 游戏窗口句柄：弹幕/字幕覆盖层要锚到「游戏画面」上（见 webview_process._overlay_rect）
        self._game_hwnd = int(game_hwnd or 0)
        self._on_closed = on_closed
        self._log_info = log_info or (lambda message: fb_log.info(message))

        self._process = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_reader = threading.Event()
        self._lock = threading.RLock()
        self._closing = False
        self._degraded = False
        self._ready = threading.Event()
        self._state = BrowserState()
        self._state_listeners: list[Callable[[BrowserState], None]] = []
        self._geometry_listeners: list[Callable[[tuple], None]] = []
        self._ui_listeners: list[Callable[[dict], None]] = []
        self._ui_events: list[dict] = []
        self._ui_lock = threading.Lock()
        self._probe_results: dict[str, Any] = {}
        self._probe_lock = threading.Lock()
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------
    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = low
        return max(low, min(high, value))

    @property
    def url(self) -> str:
        return self._url

    @property
    def width(self) -> int:
        return int(self._width)

    @property
    def height(self) -> int:
        return int(self._height)

    @property
    def opacity(self) -> float:
        return self._opacity

    @property
    def on_top_enabled(self) -> bool:
        return self._on_top

    @property
    def click_through_enabled(self) -> bool:
        return self._click_through

    @property
    def geometry(self) -> tuple[int, int, int, int]:
        """当前几何 (x, y, width, height)；位置未知时 x/y 为 -1。"""
        return (
            int(self._x) if self._x is not None else -1,
            int(self._y) if self._y is not None else -1,
            int(self._width),
            int(self._height),
        )

    @property
    def state(self) -> BrowserState:
        return self._state

    @property
    def running(self) -> bool:
        if self._degraded:
            return True
        with self._lock:
            process = self._process
        return process is not None and process.poll() is None

    @property
    def native_mode(self) -> bool:
        """是否处于真正的内嵌悬浮窗模式。"""
        return not self._degraded and self.running

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def add_state_listener(self, listener: Callable[[BrowserState], None]) -> None:
        if listener not in self._state_listeners:
            self._state_listeners.append(listener)

    def add_geometry_listener(self, listener: Callable[[tuple], None]) -> None:
        """注册窗口几何变化监听（用户拖动 / 缩放悬浮窗时触发）。"""
        if listener not in self._geometry_listeners:
            self._geometry_listeners.append(listener)

    def add_ui_listener(self, listener: Callable[[dict], None]) -> None:
        """注册悬浮工具条事件监听（透明度、穿透、置顶等）。"""
        if listener not in self._ui_listeners:
            self._ui_listeners.append(listener)

    def drain_ui_events(self) -> list[dict]:
        """取出并清空悬浮工具条事件队列（供未注册监听器时轮询）。"""
        with self._ui_lock:
            events = list(self._ui_events)
            self._ui_events.clear()
        return events

    def _publish_state(self, state: BrowserState) -> None:
        self._state = state
        for listener in list(self._state_listeners):
            try:
                listener(state)
            except Exception as error:
                fb_log.debug(f"悬浮浏览器状态监听器异常: {error}")

    def _publish_geometry(self, payload: Any) -> None:
        try:
            x, y, width, height = (int(value) for value in payload)
        except Exception:
            return
        self._x, self._y = x, y
        self._width = int(self._clamp(width, self.MIN_SIZE[0], self.MAX_SIZE[0]))
        self._height = int(self._clamp(height, self.MIN_SIZE[1], self.MAX_SIZE[1]))
        geometry = (x, y, self._width, self._height)
        for listener in list(self._geometry_listeners):
            try:
                listener(geometry)
            except Exception as error:
                fb_log.debug(f"悬浮浏览器几何监听器异常: {error}")

    def _seed_geometry(self) -> None:
        """窗口就绪后主动取一次真实几何（拿不到就交给轮询兜底）。"""
        if self._degraded:
            return
        for _ in range(10):
            geometry = self._query_geometry()
            if geometry and geometry[0] >= 0 and geometry[1] >= 0:
                self._publish_geometry(geometry)
                return
            if not self.running:
                return
            time.sleep(0.3)

    def _publish_ui(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if "opacity" in payload:
            try:
                self._opacity = self._clamp(payload["opacity"], 0.2, 1.0)
            except (TypeError, ValueError):
                pass
        if "click_through" in payload:
            self._click_through = bool(payload["click_through"])
        if "on_top" in payload:
            self._on_top = bool(payload["on_top"])
        # 工具条上的弹幕/字幕按钮也会改状态，这里把主进程的缓存跟子进程对齐
        if payload.get("mirror_danmaku") is not None or payload.get("mirror_subtitle") is not None:
            self._mirror_danmaku = bool(payload.get("mirror_danmaku"))
            self._mirror_subtitle = bool(payload.get("mirror_subtitle"))
            self._mirror = bool(self._mirror_danmaku or self._mirror_subtitle)
        with self._ui_lock:
            self._ui_events.append(dict(payload))
        for listener in list(self._ui_listeners):
            try:
                listener(payload)
            except Exception as error:
                fb_log.debug(f"悬浮浏览器工具条监听器异常: {error}")

    # ------------------------------------------------------------------
    # 能力检测
    # ------------------------------------------------------------------
    @staticmethod
    def webview_available() -> bool:
        """检测是否可以使用 WebView2 方案。"""
        if os.name != "nt":
            return False
        try:
            import webview  # noqa: F401
        except Exception:
            return False
        return FloatingBrowser._webview2_runtime_installed()

    @staticmethod
    def _webview2_runtime_installed() -> bool:
        try:
            import winreg
        except ImportError:
            return False
        key_path = (
            r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
            r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        )
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (0, winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                try:
                    with winreg.OpenKey(root, key_path, 0, winreg.KEY_READ | view) as key:
                        version, _ = winreg.QueryValueEx(key, "pv")
                        if version:
                            return True
                except OSError:
                    continue
        return False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self, wait_ready: float = 8.0) -> bool:
        """启动悬浮浏览器子进程，返回是否进入内嵌 WebView2 模式。"""
        with self._lock:
            if self.running:
                self.log("悬浮浏览器已经在运行")
                return self.native_mode
            self._closing = False
            self._ready.clear()

        if not self.webview_available():
            self._start_degraded()
            return False

        try:
            # 用独立解释器（subprocess）+ stdin/stdout 管道，而不是 multiprocessing。
            # 原因见 _launcher.py 顶部注释：spawn 在 Qt 宿主下会静默卡死。
            launcher = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_launcher.py")
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__)))))
            payload = {
                "url": self._url,
                "width": self._width,
                "height": self._height,
                "opacity": self._opacity,
                "x": self._x,
                "y": self._y,
                "on_top": self._on_top,
                "click_through": self._click_through,
                "hover_opacity": self._hover_opacity,
                "mirror": bool(self._mirror),
                "mirror_danmaku": bool(self._mirror_danmaku),
                "mirror_subtitle": bool(self._mirror_subtitle),
                "game_hwnd": int(self._game_hwnd or 0),
                # 日志总开关：子进程据此决定要不要往 stderr 写诊断信息
                "verbose": fb_log.is_verbose(),
            }
            environment = os.environ.copy()
            environment["OK_FLOATING_BROWSER_CONFIG"] = json.dumps(
                payload, ensure_ascii=False
            )
            environment["PYTHONIOENCODING"] = "utf-8"

            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(
                [sys.executable, "-u", launcher],
                cwd=project_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                creationflags=creation_flags,
            )
            with self._lock:
                self._process = process
            with _LIVE_LOCK:
                _LIVE_PROCESSES.add(process)

            self._stop_reader.clear()
            self._reader_thread = threading.Thread(
                target=self._read_status_loop, name="FloatingBrowserStatus", daemon=True
            )
            self._reader_thread.start()
            threading.Thread(
                target=self._read_stderr_loop, name="FloatingBrowserStderr", daemon=True
            ).start()
        except Exception as error:
            # 日志可能被总开关静音，失败原因同时走界面提示通道
            self.log(f"启动悬浮浏览器子进程失败: {error}")
            fb_log.error(f"启动悬浮浏览器子进程失败: {error}")
            self._last_error = str(error)
            self._start_degraded()
            return False

        if self._ready.wait(wait_ready) and not self._degraded:
            self.log(
                f"悬浮浏览器已启动（WebView2）: {self._width}x{self._height} "
                f"透明度 {self._opacity:.2f}"
            )
            return True

        if self._degraded:
            return False

        # 进程还活着，只是初始化慢，继续等待但不阻塞调用方
        self.log("悬浮浏览器进程启动中，窗口稍后出现")
        return True

    def _read_stderr_loop(self) -> None:
        """把子进程 stderr 收进日志，便于排查。

        关键信息（Traceback / Exception / Error）提升到 WARNING，否则在
        默认 INFO 级别下会被埋没，子进程崩溃了都不知道原因。
        """
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for raw in iter(process.stderr.readline, b""):
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                lowered = text.lower()
                if any(mark in lowered for mark in ("traceback", "exception", "error", "fail")):
                    fb_log.warning(f"[悬浮浏览器子进程] {text}")
                else:
                    fb_log.debug(f"[悬浮浏览器子进程] {text}")
        except Exception:
            pass

    def _read_status_loop(self) -> None:
        """读取子进程状态（stdout 上的按行 JSON）。"""
        process = self._process
        if process is None or process.stdout is None:
            return
        stream = process.stdout
        while not self._stop_reader.is_set():
            try:
                raw = stream.readline()
            except Exception:
                break
            if not raw:
                # 子进程退出（stdout 到 EOF）。正常关闭会先收到 closed 消息；
                # 这里兜底「意外崩溃」：进程没了但没发 closed。
                if self.running:
                    time.sleep(0.2)
                    continue
                self._handle_process_closed()
                break
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            # 只处理带协议前缀的行，其余（第三方库的打印）忽略
            if not text.startswith(PROTOCOL_PREFIX):
                fb_log.debug(f"[悬浮浏览器子进程输出] {text[:200]}")
                continue
            text = text[len(PROTOCOL_PREFIX):]
            try:
                message = json.loads(text)
            except Exception:
                fb_log.debug(f"子进程输出无法解析: {text[:200]}")
                continue

            kind = message.get("kind")
            payload = message.get("payload")

            if kind == "state":
                self._publish_state(BrowserState.from_payload(payload))
            elif kind == "geometry":
                self._publish_geometry(payload)
            elif kind == "ui":
                self._publish_ui(payload)
            elif kind == "eval_result":
                try:
                    request_id, value = payload
                except (TypeError, ValueError):
                    continue
                with self._probe_lock:
                    self._probe_results[request_id] = value
            elif kind == "inspect_result":
                try:
                    request_id, value = payload
                except (TypeError, ValueError):
                    continue
                with self._probe_lock:
                    self._probe_results[request_id] = value
            elif kind == "ready":
                self._ready.set()
                # 窗口就绪后立刻拉一次真实几何，避免调用方读到 (-1, -1) 占位值
                threading.Thread(target=self._seed_geometry, daemon=True).start()
            elif kind == "error":
                self._last_error = str(payload)
                # 同上：错误要同时让用户在界面上看到
                self.log(f"悬浮浏览器子进程错误: {payload}")
                fb_log.error(f"悬浮浏览器子进程报告错误: {payload}")
            elif kind == "closed":
                self._handle_process_closed()

    def _handle_process_closed(self) -> None:
        with self._lock:
            if self._closing or self._process is None:
                return
            self._process = None
            self._degraded = False
        self.log("悬浮浏览器窗口已关闭")
        if self._on_closed is not None:
            try:
                self._on_closed()
            except Exception as error:
                fb_log.debug(f"on_closed 回调异常: {error}")

    # ------------------------------------------------------------------
    # 指令下发
    # ------------------------------------------------------------------
    def _send(self, command: str, argument: Any = None) -> bool:
        """通过 stdin 把指令发给子进程。"""
        with self._lock:
            process = self._process
        if process is None or process.stdin is None:
            return False
        try:
            line = json.dumps({"cmd": command, "arg": argument}, ensure_ascii=False)
            process.stdin.write((line + "\n").encode("utf-8"))
            process.stdin.flush()
            return True
        except Exception as error:
            fb_log.debug(f"下发指令 {command} 失败: {error}")
            return False

    def probe_js(self, script: str, timeout: float = 4.0) -> Any:
        """在悬浮窗页面内执行一段 JS 并取回结果（同步等待）。

        仅用于诊断与自测：``script`` 需要是以 ``; true;`` 结尾的表达式，
        返回值经 ``json.loads`` 解析，失败时返回 ``None``。
        """
        if not self.running or self._degraded:
            return None
        request_id = uuid.uuid4().hex
        with self._probe_lock:
            self._probe_results.pop(request_id, None)
        if not self._send("eval_js", (request_id, script)):
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._probe_lock:
                if request_id in self._probe_results:
                    return self._probe_results.pop(request_id)
            time.sleep(0.05)
        return None

    def _query(self, command: str, argument: Any = None, timeout: float = 3.0) -> Any:
        """向子进程发一个 ``inspect_result`` 类查询并等结果，失败返回 None。"""
        if not self.running or self._degraded:
            return None
        request_id = uuid.uuid4().hex
        with self._probe_lock:
            self._probe_results.pop(request_id, None)
        payload = (request_id, argument) if argument is not None else request_id
        if not self._send(command, payload):
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._probe_lock:
                if request_id in self._probe_results:
                    return self._probe_results.pop(request_id)
            time.sleep(0.05)
        return None

    def _query_geometry(self, timeout: float = 3.0) -> Any:
        """向子进程索取当前窗口真实矩形，失败返回 None。"""
        return self._query("get_geometry", timeout=timeout)

    def inspect(self, timeout: float = 4.0) -> Any:
        """读取子进程内部真实状态（诊断用）。

        与 :meth:`probe_js` 不同，这里返回的是子进程内存里的
        ``_state`` 快照（hwnd / click_through / on_top / opacity），
        用于判断「主进程以为的状态」与「子进程实际状态」是否一致。
        """
        if not self.running or self._degraded:
            return None
        request_id = uuid.uuid4().hex
        with self._probe_lock:
            self._probe_results.pop(request_id, None)
        if not self._send("inspect_state", request_id):
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._probe_lock:
                if request_id in self._probe_results:
                    return self._probe_results.pop(request_id)
            time.sleep(0.05)
        return None

    def set_opacity(self, opacity: float) -> float:
        """设置窗口整体不透明度（0.2 ~ 1.0）。"""
        self._opacity = self._clamp(opacity, 0.2, 1.0)
        self._send("set_opacity", self._opacity)
        self.log(f"悬浮浏览器透明度: {self._opacity:.2f}")
        return self._opacity

    def set_hover_opacity(self, opacity: float) -> float:
        """设置穿透模式下鼠标悬停时的透明度（0.05 ~ 1.0）。"""
        self._hover_opacity = self._clamp(opacity, 0.05, 1.0)
        self._send("set_hover_opacity", self._hover_opacity)
        return self._hover_opacity

    def set_size(self, width: int, height: int) -> tuple[int, int]:
        """调整窗口尺寸。"""
        self._width = int(self._clamp(width, self.MIN_SIZE[0], self.MAX_SIZE[0]))
        self._height = int(self._clamp(height, self.MIN_SIZE[1], self.MAX_SIZE[1]))
        self._send("set_size", (self._width, self._height))
        self.log(f"悬浮浏览器尺寸: {self._width}x{self._height}")
        return self._width, self._height

    def set_geometry(self, x: int, y: int, width: int, height: int) -> tuple[int, int, int, int]:
        """同时设置位置与尺寸（不主动回写，避免与拖动互相打断）。"""
        self._x, self._y = int(x), int(y)
        self._width = int(self._clamp(width, self.MIN_SIZE[0], self.MAX_SIZE[0]))
        self._height = int(self._clamp(height, self.MIN_SIZE[1], self.MAX_SIZE[1]))
        self._send("set_geometry", (self._x, self._y, self._width, self._height))
        return self.geometry

    def set_position(self, x: int, y: int) -> None:
        """移动窗口到指定坐标。"""
        self._x, self._y = int(x), int(y)
        self._send("set_position", (self._x, self._y))

    def set_click_through(self, enabled: bool) -> bool:
        """开启后鼠标点击直接穿透悬浮窗落到下层窗口。"""
        self._click_through = bool(enabled)
        self._send("set_click_through", self._click_through)
        self.log(f"悬浮浏览器鼠标穿透: {'开启' if self._click_through else '关闭'}")
        return self._click_through

    def toggle_click_through(self) -> bool:
        return self.set_click_through(not self._click_through)

    def mirror_debug(self, timeout: float = 3.0) -> Any:
        """测试/诊断用：读取覆盖层状态（窗口句柄、置顶、最后绘制内容等）。"""
        return self._query("inspect_mirror", timeout=timeout)

    def debug_load_danmaku(self, items: list) -> None:
        """测试/诊断用：跳过网络直接把弹幕灌进引擎（离线验证自绘渲染）。"""
        self._send("debug_load_danmaku", list(items or []))

    def debug_set_playback(self, seconds: float, rate: float = 1.0,
                           paused: bool = False) -> None:
        """测试/诊断用：直接设定播放时刻（不走页面上报）。"""
        self._send("debug_set_playback", [float(seconds), float(rate), bool(paused)])

    def debug_set_overlay_settings(self, kind: str, settings: dict) -> None:
        """测试/诊断用：直接改覆盖层设置（不经过设置面板）。"""
        self._send("debug_set_settings",
                   {"kind": kind, "settings": dict(settings or {})})

    def set_mirror(self, enabled: bool) -> bool:
        """总开关（工具条按钮 / 热键用）：同时开/关弹幕与字幕。

        覆盖层铺满**游戏画面**，位置按比例铺开、字号不缩放（跟页面里一样）。
        """
        self._mirror = bool(enabled)
        self._mirror_danmaku = bool(enabled)
        self._mirror_subtitle = bool(enabled)
        self._send("set_mirror", self._mirror)
        self.log(f"弹幕/字幕映射: {'开启' if self._mirror else '关闭'}")
        return self._mirror

    def set_mirror_danmaku(self, enabled: bool) -> bool:
        """单独开关「弹幕」映射。"""
        self._mirror_danmaku = bool(enabled)
        self._mirror = bool(self._mirror_danmaku or self._mirror_subtitle)
        self._send("set_mirror_danmaku", self._mirror_danmaku)
        self.log(f"弹幕映射: {'开启' if self._mirror_danmaku else '关闭'}")
        return self._mirror_danmaku

    def set_mirror_subtitle(self, enabled: bool) -> bool:
        """单独开关「字幕」映射。"""
        self._mirror_subtitle = bool(enabled)
        self._mirror = bool(self._mirror_danmaku or self._mirror_subtitle)
        self._send("set_mirror_subtitle", self._mirror_subtitle)
        self.log(f"字幕映射: {'开启' if self._mirror_subtitle else '关闭'}")
        return self._mirror_subtitle

    @property
    def mirror_danmaku_enabled(self) -> bool:
        return self._mirror_danmaku

    @property
    def mirror_subtitle_enabled(self) -> bool:
        return self._mirror_subtitle

    def set_game_hwnd(self, hwnd: int) -> None:
        """告诉子进程游戏窗口句柄（弹幕覆盖层的锚点）。

        子进程自己也会按「窗口类名 + 进程名」去找，这里推过去的是 ok 框架
        (``device_manager``) 已经解析好的句柄，作为兜底；两者都没有时覆盖层
        退回跟随悬浮窗。
        """
        hwnd = int(hwnd or 0)
        if hwnd == self._game_hwnd:
            return
        self._game_hwnd = hwnd
        self._send("set_game_hwnd", hwnd)

    @property
    def mirror_enabled(self) -> bool:
        return self._mirror

    def set_on_top(self, on_top: bool) -> None:
        """切换置顶状态。"""
        self._on_top = bool(on_top)
        self._send("set_on_top", self._on_top)

    def show(self) -> None:
        self._send("show")

    def hide(self) -> None:
        self._send("hide")

    def toggle_visible(self) -> None:
        # 穿透状态下无法点击悬浮窗，显示时顺手恢复交互能力
        if self._click_through:
            self._click_through = False
            self._send("set_interactive", True)
        self._send("toggle_visible")

    def load_url(self, url: str) -> None:
        """在悬浮窗中打开新地址。"""
        if not url:
            return
        self._url = url
        if self._degraded:
            self._open_in_default_browser(url)
        else:
            self._send("load_url", url)

    # ------------------------------------------------------------------
    # 视频控制
    # ------------------------------------------------------------------
    def refresh_state(self) -> BrowserState:
        """请求并返回最新的视频状态。"""
        if self._degraded:
            return self._state
        self._send("refresh")
        return self._state

    def install_video_hooks(self) -> None:
        """重新注入视频控制脚本（页面跳转后调用）。"""
        self._send("refresh")

    def play_pause(self) -> BrowserState:
        self._send("play_pause")
        return self._state

    def seek(self, seconds: float) -> BrowserState:
        """相对当前时间快进（正数）或后退（负数）。"""
        self._send("seek", float(seconds))
        return self._state

    def speed_up(self, step: float = 0.25, maximum: float = 4.0) -> BrowserState:
        self._send("speed_up")
        return self._state

    def slow_down(self, step: float = 0.25, minimum: float = 0.25) -> BrowserState:
        self._send("slow_down")
        return self._state

    def set_rate(self, rate: float) -> BrowserState:
        self._send("set_rate", float(rate))
        return self._state

    def set_hold_rate(self, rate: float) -> BrowserState:
        """「按住倍速」：以指定倍速播放，并在 JS 侧记住原倍速。

        真正的「记住原倍速 / 松开恢复」由页面脚本完成，主进程缓存的状态可能
        滞后（例如页面刚跳转还没轮询到），放在 JS 里才不会出现「松手后卡在 3
        倍速」或「按住没反应」。
        """
        self._send("hold_rate", float(rate))
        return self._state

    def release_hold_rate(self) -> BrowserState:
        """松开「按住倍速」：恢复按住之前的倍速。"""
        self._send("release_hold")
        return self._state

    def set_volume(self, volume: float) -> BrowserState:
        self._send("set_volume", float(volume))
        return self._state

    def volume_up(self, step: float = 0.1) -> BrowserState:
        self._send("volume_up", float(step))
        return self._state

    def volume_down(self, step: float = 0.1) -> BrowserState:
        self._send("volume_down", float(step))
        return self._state

    def toggle_mute(self) -> BrowserState:
        self._send("toggle_mute")
        return self._state

    def handle_action(self, action: str, value: float | None = None) -> BrowserState:
        """统一动作分发入口，供热键与界面按钮调用。"""
        mapping = {
            "play_pause": lambda: self.play_pause(),
            "forward": lambda: self.seek(value if value is not None else 5),
            "backward": lambda: self.seek(-(value if value is not None else 5)),
            "volume_up": lambda: self.volume_up(value if value is not None else 0.1),
            "volume_down": lambda: self.volume_down(value if value is not None else 0.1),
            "toggle_click_through": lambda: (self.toggle_click_through(), self._state)[1],
            "toggle_mirror": lambda: (self.set_mirror(not self._mirror), self._state)[1],
        }
        handler = mapping.get(action)
        if handler is None:
            fb_log.debug(f"未知的悬浮浏览器动作: {action}")
            return self._state
        state = handler()
        self.log(f"悬浮浏览器动作 {action} -> {self.state_text(state)}")
        return state

    def state_text(self, state: Optional[BrowserState] = None) -> str:
        state = state or self._state
        if not state.found:
            return "未找到视频"
        flag = "暂停" if state.paused else "播放"
        return (
            f"{flag} {_format_time(state.current)}/{_format_time(state.duration)} "
            f"{state.rate:.2f}x"
        )

    # ------------------------------------------------------------------
    # 降级模式
    # ------------------------------------------------------------------
    def _start_degraded(self) -> None:
        """无 WebView2 时，直接用系统浏览器打开视频页面。"""
        with self._lock:
            self._degraded = True
        self._last_error = "未检测到 WebView2 运行时，已降级为系统浏览器模式"
        self.log(self._last_error)
        if self._url and self._url != "about:blank":
            self._open_in_default_browser(self._url)

    @staticmethod
    def _open_in_default_browser(url: str) -> None:
        try:
            os.startfile(url)  # noqa: S606 - Windows 专用
        except Exception as error:
            fb_log.error(f"打开系统浏览器失败: {error}")

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    def stop(self, timeout: float = 3.0) -> None:
        """关闭悬浮浏览器。"""
        with self._lock:
            self._closing = True
            process = self._process
        self._send("quit")
        self._stop_reader.set()

        if process is not None:
            try:
                process.wait(timeout=timeout)
            except Exception:
                fb_log.debug("悬浮浏览器进程未及时退出，强制结束进程树")
                _kill_process_tree(process.pid)
                try:
                    process.wait(timeout=2)
                except Exception:
                    pass
            with _LIVE_LOCK:
                _LIVE_PROCESSES.discard(process)

        for stream in (
            getattr(self._process, "stdin", None),
            getattr(self._process, "stdout", None),
            getattr(self._process, "stderr", None),
        ):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

        with self._lock:
            self._process = None
            self._degraded = False
            self._closing = False
        self._ready.clear()
        self.log("悬浮浏览器已关闭")

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def log(self, message: str) -> None:
        try:
            self._log_info(message)
        except Exception:
            fb_log.info(message)

    def snapshot(self) -> dict:
        """返回当前状态快照，便于界面展示。"""
        payload = asdict(self._state)
        x, y, width, height = self.geometry
        payload.update(
            {
                "url": self._url,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "opacity": self._opacity,
                "on_top": self._on_top,
                "click_through": self._click_through,
                "running": self.running,
                "native": self.native_mode,
                "degraded": self._degraded,
                "error": self._last_error,
            }
        )
        return payload


def normalize_url(raw: str) -> str:
    """把用户输入整理成可加载的地址。

    - 空字符串 -> bilibili 首页
    - 已带协议 -> 原样返回
    - 形如路径且文件存在 -> 转成 file:// URL
    - 其它 -> 补上 https://
    """
    raw = (raw or "").strip()
    if not raw:
        return "https://www.bilibili.com/"
    if re.match(r"^(https?|file|about):", raw, re.IGNORECASE):
        return raw
    if os.path.exists(raw):
        path = os.path.abspath(raw).replace("\\", "/")
        return "file:///" + quote(path, safe="/:")
    return "https://" + raw
