"""弹幕 / 字幕的原生顶层覆盖层（Win32 分层窗口 + GDI 画字）。

为什么不复用 WebView2 做覆盖层
----------------------------
实测 ``pywebview`` 的 ``transparent=True`` 在这个环境里**无效**：
它只是把窗体背景色设成纯红并启用 ``TransparencyKey``，但 WebView2 是**独立子窗口**，
颜色键管不到它自己的像素；而 ``DefaultBackgroundColor`` 虽然已经是
``Color [A=0, R=255, G=255, B=255]``（即 Transparent），在**窗口化(非 visual hosting)**
模式下 WebView2 依然按不透明合成 —— 截图验证：整块白色盖住下层窗口。

所以这里直接自己开一个 Win32 窗口：

- ``WS_EX_LAYERED`` + ``SetLayeredWindowAttributes(key=纯黑, LWA_COLORKEY)``
  → 画面里**纯黑**的像素全部透明，其余像素正常显示（真正的逐像素抠图）。
  颜色键用**纯黑**而不是品红：文字抗锯齿的边缘像素是「描边色 ↔ 颜色键」的混合，
  黑键的混合结果还是黑（看不见），品红键会混出一圈紫边（用户实测反馈过）。
  代价是描边色不能是真黑，用近黑 (10,10,10)。
- 每帧在内存 DC 里铺满品红，然后用 GDI 画文字：先按同半径的圆盘偏移铺一层黑色
  「描边」（B 站是 ``text-shadow: 0 0 1px`` 的无偏移黑晕，GDI 没有文字模糊，用多张
  偏移近似），再画本体。描边让抗锯齿边缘落在「黑 ↔ 品红」之间（暗色），几乎看不出
  杂边，同时也让弹幕在任何游戏画面上都能看清。
- ``WS_EX_TRANSPARENT`` → 完全不挡鼠标，点击直接落到游戏上。
- ``WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE`` → 不出现在任务栏、不抢焦点。

坐标：``set_content`` 收到的是**页面视口坐标**，``set_viewport`` 告知视口大小，
绘制时按 ``覆盖层尺寸 ÷ 视口尺寸`` 换算 —— **只换算位置**（x/y 与字幕的居中点 cx），
于是弹幕铺满整个游戏窗口；**字号 / 字体 / 字重 / 描边 / 颜色一律用站点原值不缩放**
（用户要求「字体除了位置以外其他样式都不变」）。
字体名 / 字重 / 描边宽度都取自站点的计算样式（见 ``MIRROR_JS`` 里的 ``readStyle``），
所以画出来跟 B 站播放器里的样式一致（实测 B 站是 SimHei / 700 / 0 0 1px 黑）。
字幕按页面里的**文字中心**对齐绘制（页面用 ``Range`` 量出真实文字矩形），
因为字幕容器比文字宽、页面里是居中的。覆盖层的位置与尺寸由调用方决定 ——
见 ``webview_process._overlay_rect``。
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Win32 常量
# ---------------------------------------------------------------------------
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000

GWL_EXSTYLE = -20
# HWND_TOPMOST 不再用于本模块（见 _sync_and_draw 的注释）
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
SWP_SHOWWINDOW = 0x0040
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

LWA_COLORKEY = 0x00000001
# 颜色键：**纯黑**。为什么不用品红（0xFF00FF）？因为颜色键没法做半透明，
# 文字抗锯齿的边缘像素是「描边色 ↔ 颜色键」的混合：用黑当键时混合结果还是黑，
# 看不见；用品红当键时混合结果是**紫色**，会在每个字外面形成一圈紫边
# （实测用户反馈「字幕边缘有紫色边框」就是这个）。
# 纯黑做键的代价是描边色不能是真黑（会被一起抠掉），所以描边用 (10,10,10)。
COLOR_KEY = 0x00000000
# 描边色：近黑（10,10,10），比纯黑高一点点，避免被颜色键抠掉
STROKE_COLOR = 0x000A0A0A
# 文字颜色太接近颜色键时会被抠没，这里抬一下（肉眼无差别）
_MIN_COLOR = 13

SRCCOPY = 0x00CC0020
TRANSPARENT = 1
FW_BOLD = 700
FW_NORMAL = 400
ANTIALIASED_QUALITY = 4
DEFAULT_CHARSET = 1
OUT_TT_PRECIS = 5
CLIP_DEFAULT_PRECIS = 0
DEFAULT_PITCH = 0
FF_DONTCARE = 0
DT_SINGLELINE = 0x00000020
DT_NOCLIP = 0x00000100
DT_CALCRECT = 0x00000400
DT_NOPREFIX = 0x00000800

FONT_FACE = "Microsoft YaHei"

# 描边：B 站用的是 ``text-shadow: rgb(0,0,0) 0px 0px 1px``（无偏移的 1px 黑晕），
# GDI 没有文字模糊，用「同一个半径的圆盘偏移各画一遍黑字、最后叠一层本色」来近似。
# 半径按 ``sy`` 等比放大（= 把同一个字形整体放大，而不是像浏览器那样固定 1px）。
# 上限 3：圆盘偏移数是 O(r²)，4K 下 sy≈3 已经够用，再大只是白白烧 GPU/CPU。
MAX_OUTLINE = 3


def _stroke_offsets(radius: int) -> tuple[tuple[int, int], ...]:
    r = max(1, int(radius))
    offsets = []
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx == 0 and dy == 0:
                continue
            if dx * dx + dy * dy <= r * r:
                offsets.append((dx, dy))
    return tuple(offsets) or ((0, 0),)


_STROKE_OFFSETS = _stroke_offsets(1)


if os.name == "nt":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    _kernel32.GetModuleHandleW.restype = ctypes.c_void_p

    _user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    _user32.RegisterClassW.restype = ctypes.c_uint
    _user32.CreateWindowExW.argtypes = [
        ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _user32.CreateWindowExW.restype = ctypes.c_void_p
    _user32.DestroyWindow.argtypes = [ctypes.c_void_p]
    _user32.DestroyWindow.restype = ctypes.c_int
    _user32.DefWindowProcW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.SetLayeredWindowAttributes.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_uint,
    ]
    _user32.SetLayeredWindowAttributes.restype = ctypes.c_int
    _user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
    _user32.SetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    _user32.SetWindowPos.restype = ctypes.c_int
    _user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.ShowWindow.restype = ctypes.c_int
    _user32.PeekMessageW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
    ]
    _user32.PeekMessageW.restype = ctypes.c_int
    _user32.TranslateMessage.argtypes = [ctypes.c_void_p]
    _user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
    _user32.GetDC.argtypes = [ctypes.c_void_p]
    _user32.GetDC.restype = ctypes.c_void_p
    _user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.IsWindowVisible.restype = ctypes.c_int
    _user32.FillRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _user32.FillRect.restype = ctypes.c_int
    _user32.DrawTextW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_uint,
    ]
    _user32.DrawTextW.restype = ctypes.c_int
    _user32.BeginPaint.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.BeginPaint.restype = ctypes.c_void_p
    _user32.EndPaint.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.EndPaint.restype = ctypes.c_int
    _user32.InvalidateRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    _user32.InvalidateRect.restype = ctypes.c_int
    _user32.UpdateWindow.argtypes = [ctypes.c_void_p]
    _user32.UpdateWindow.restype = ctypes.c_int

    _gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    _gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    _gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    _gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    _gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _gdi32.SelectObject.restype = ctypes.c_void_p
    _gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    _gdi32.DeleteObject.restype = ctypes.c_int
    _gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    _gdi32.DeleteDC.restype = ctypes.c_int
    _gdi32.CreateSolidBrush.argtypes = [ctypes.c_uint]
    _gdi32.CreateSolidBrush.restype = ctypes.c_void_p
    _gdi32.CreateFontW.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
        ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_wchar_p,
    ]
    _gdi32.CreateFontW.restype = ctypes.c_void_p
    _gdi32.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _gdi32.SetBkMode.restype = ctypes.c_int
    _gdi32.SetTextColor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _gdi32.SetTextColor.restype = ctypes.c_uint
    _gdi32.BitBlt.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    _gdi32.BitBlt.restype = ctypes.c_int
else:  # pragma: no cover - 非 Windows
    _user32 = _gdi32 = _kernel32 = None


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", ctypes.c_void_p),
        ("fErase", ctypes.c_int),
        ("rcPaint", wintypes.RECT),
        ("fRestore", ctypes.c_int),
        ("fIncUpdate", ctypes.c_int),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p
)


def _nudge_color(value: Any) -> int:
    """CSS 颜色字符串 -> COLORREF(0x00BBGGRR)。

    颜色键是纯黑，所以「太黑」的文字会被抠没，这里统一抬到 ``_MIN_COLOR``
    （肉眼分不出 13 和 0 的区别，但不会被抠掉）。
    """
    red = green = blue = 255
    text = str(value or "").strip()
    if text.startswith("rgb"):
        numbers = []
        for chunk in text[text.find("(") + 1: text.rfind(")")].split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                numbers.append(float(chunk))
            except ValueError:
                numbers.append(0.0)
        if len(numbers) >= 3:
            red, green, blue = (max(0, min(255, int(round(n)))) for n in numbers[:3])
    red, green, blue = (max(_MIN_COLOR, value) for value in (red, green, blue))
    return (blue << 16) | (green << 8) | red


class _FontCache:
    """按 (字体名, 是否加粗, 像素高度) 缓存 GDI 字体对象。"""

    def __init__(self) -> None:
        self._fonts: dict[tuple[str, bool, int], int] = {}

    def get(self, pixels: int, family: str = "", bold: bool = True) -> int:
        size = max(8, min(300, int(pixels)))
        name = (family or FONT_FACE).strip().strip('"').strip("'")
        key = (name, bool(bold), size)
        font = self._fonts.get(key)
        if font:
            return font
        weight = FW_BOLD if bold else FW_NORMAL
        handle = _gdi32.CreateFontW(
            -size, 0, 0, 0, weight, 0, 0, 0,
            DEFAULT_CHARSET, OUT_TT_PRECIS, CLIP_DEFAULT_PRECIS,
            ANTIALIASED_QUALITY, DEFAULT_PITCH | FF_DONTCARE, name,
        )
        if not handle:
            # 站点给的字体本机没有（或不是中文字体）时退回雅黑，再退黑体
            for fallback in (FONT_FACE, "SimHei"):
                handle = _gdi32.CreateFontW(
                    -size, 0, 0, 0, weight, 0, 0, 0,
                    DEFAULT_CHARSET, OUT_TT_PRECIS, CLIP_DEFAULT_PRECIS,
                    ANTIALIASED_QUALITY, DEFAULT_PITCH | FF_DONTCARE, fallback,
                )
                if handle:
                    break
        self._fonts[key] = handle
        return handle

    def clear(self) -> None:
        for font in self._fonts.values():
            try:
                _gdi32.DeleteObject(font)
            except Exception:
                pass
        self._fonts.clear()


class DanmakuOverlay:
    """顶层透明的弹幕画布。

    线程模型：``start()`` 起一个专用线程创建窗口 + 泵消息 + 按需重绘；
    外部只需要 ``set_content()`` / ``set_geometry()`` / ``set_visible()``，
    不需要碰 Win32。

    坐标：``set_content`` 收的是**页面视口坐标**，``set_viewport`` 告知视口大小，
    绘制时按 ``覆盖层尺寸 / 视口尺寸`` 把**位置**换算到覆盖层坐标 —— 弹幕因此铺满
    整个游戏窗口；而**字号 / 字重 / 字体 / 描边 / 颜色一律用站点原值，不缩放**。
    """

    CLASS_NAME = "OKWWDmakuOverlayCanvas"

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._hwnd = 0
        self._lock = threading.RLock()
        self._dirty = True
        self._visible = False
        self._items: list[dict] = []
        self._subtitle: Optional[dict] = None
        self._geometry = (0, 0, 0, 0)
        self._viewport = (0, 0)          # 页面视口尺寸（源坐标空间）
        self._style: dict = {}           # {"family": str, "bold": bool, "blur": float}
        self._fonts = _FontCache()
        self._shown = False
        self._drawn_geometry = (0, 0, 0, 0)
        self._last_draw = None
        self._last_scale = (1.0, 1.0)
        self._last_outline = 1
        self._last_draw_ms = 0.0   # 最近一次绘制耗时（诊断/调优用）
        self._frame = ((0, 0, 0, 0), [], None, 1)
        self._buffer = None              # (width, height, mem_dc, bitmap) 复用，避免每帧重建
        self._last_error = ""
        self._wndproc_ref = None  # 防 GC

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def hwnd(self) -> int:
        return self._hwnd

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_error(self) -> str:
        return self._last_error

    def start(self) -> bool:
        if os.name != "nt":
            self._last_error = "非 Windows 平台"
            return False
        with self._lock:
            if self.running:
                return True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="FloatingBrowserDanmakuOverlay", daemon=True
            )
            self._thread.start()
        # 等窗口建好（最多 2 秒）
        for _ in range(40):
            if self._hwnd or not self.running:
                break
            self._stop_event.wait(0.05)
        return bool(self._hwnd)

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        self._hwnd = 0

    # ------------------------------------------------------------------
    # 外部接口
    # ------------------------------------------------------------------
    def set_geometry(self, x: int, y: int, width: int, height: int) -> None:
        rect = (int(x), int(y), max(1, int(width)), max(1, int(height)))
        with self._lock:
            if rect == self._geometry:
                return
            self._geometry = rect
            self._dirty = True

    def set_content(self, items: Optional[list], subtitle: Optional[dict]) -> None:
        with self._lock:
            self._items = list(items or [])
            self._subtitle = subtitle or None
            self._dirty = True

    def set_viewport(self, width: int, height: int) -> None:
        """页面视口尺寸（源坐标空间）。``覆盖层尺寸 / 视口尺寸`` = 位置映射比例。"""
        size = (max(1, int(width or 0)), max(1, int(height or 0)))
        with self._lock:
            if size == self._viewport:
                return
            self._viewport = size
            self._dirty = True

    def set_style(self, style: Optional[dict]) -> None:
        """站点里弹幕的字体/字重/描边（来自页面计算样式，保证样式一致）。"""
        style = dict(style or {})
        with self._lock:
            if style == self._style:
                return
            self._style = style
            self._dirty = True

    def set_visible(self, visible: bool) -> None:
        with self._lock:
            self._visible = bool(visible)
            self._dirty = True

    def debug_info(self) -> dict:
        """诊断用：窗口矩形、客户区矩形、最后一次绘制的参数等。"""
        hwnd = self._hwnd
        info = {"hwnd": hwnd, "shown": self._shown, "last_draw": self._last_draw,
                "geometry": self._geometry,
                "viewport": self._viewport,
                "scale": list(self._last_scale),
                "style": dict(self._style),
                "outline": self._last_outline, "draw_ms": self._last_draw_ms,
                "last_error": self._last_error}
        # 变换前后的前几条内容：便于断言「画到哪、字号多大」
        try:
            info["frame_items"] = [dict(item) for item in (self._frame[1] or [])[:3]]
            info["raw_items"] = [dict(item) for item in (self._items or [])[:3]]
            info["raw_subtitle"] = dict(self._subtitle) if self._subtitle else None
        except Exception:
            info["frame_items"] = []
            info["raw_items"] = []
            info["raw_subtitle"] = None
        if hwnd and os.name == "nt":
            rect = wintypes.RECT()
            _user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            _user32.GetWindowRect.restype = ctypes.c_int
            if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                info["rect"] = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
            client = wintypes.RECT()
            _user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            _user32.GetClientRect.restype = ctypes.c_int
            if _user32.GetClientRect(hwnd, ctypes.byref(client)):
                info["client"] = (client.right, client.bottom)
        return info

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _run(self) -> None:
        user32 = _user32
        hmod = _kernel32.GetModuleHandleW(None)
        self._wndproc_ref = WNDPROC(self._on_message)

        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p).value
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hmod
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = self.CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(wc)):
            error = ctypes.get_last_error()
            if error not in (1410,):  # 1410 = 类已存在（重复 start）
                self._last_error = f"RegisterClassW 失败 err={error}"
                return

        ex_style = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
                    | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
        hwnd = user32.CreateWindowExW(
            ex_style, self.CLASS_NAME, "OKWW Danmaku Overlay", WS_POPUP,
            -32000, -32000, 10, 10, None, None, hmod, None,
        )
        if not hwnd:
            self._last_error = f"CreateWindowExW 失败 err={ctypes.get_last_error()}"
            return
        self._hwnd = int(hwnd)
        # 颜色键抠图：品红像素全部透明
        user32.SetLayeredWindowAttributes(hwnd, COLOR_KEY, 0, LWA_COLORKEY)
        user32.SetWindowPos(hwnd, None, -32000, -32000, 10, 10,
                            SWP_NOACTIVATE | SWP_NOZORDER)

        msg = wintypes.MSG()
        while not self._stop_event.is_set():
            try:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                self._sync_and_draw()
            except Exception as error:
                self._last_error = repr(error)
            self._stop_event.wait(0.012)

        try:
            user32.DestroyWindow(hwnd)
        except Exception:
            pass
        self._hwnd = 0
        self._release_buffer()
        self._fonts.clear()

    def _on_message(self, hwnd, message, wparam, lparam):
        if message == 0x000F:      # WM_PAINT
            self._paint(hwnd)
            return 0
        if message == 0x0014:      # WM_ERASEBKGND：颜色键窗口不需要系统擦背景
            return 1
        return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _paint(self, hwnd) -> None:
        """WM_PAINT 里绘制。

        必须走这里而不是直接往窗口 DC 上 BitBlt：实测直接 BitBlt 时分层窗口
        只有一部分区域会被合成上屏（下半部分不显示），走标准绘制模型才完整。
        """
        paint = PAINTSTRUCT()
        hdc = _user32.BeginPaint(hwnd, ctypes.byref(paint))
        if not hdc:
            return
        try:
            geometry, items, subtitle, outline = self._frame
            width, height = int(geometry[2]), int(geometry[3])
            if width > 0 and height > 0:
                self._draw(hdc, width, height, items, subtitle, outline)
        finally:
            _user32.EndPaint(hwnd, ctypes.byref(paint))

    def _scale_for(self, geometry) -> tuple[float, float]:
        """``覆盖层尺寸 / 页面视口尺寸`` —— 位置映射比例（字号不参与）。"""
        view = self._viewport
        if not view or view[0] <= 0 or view[1] <= 0:
            return (1.0, 1.0)
        return (float(geometry[2]) / view[0], float(geometry[3]) / view[1])

    def _map_items(self, items: list, sx: float, sy: float) -> list:
        """把页面视口坐标**换算到覆盖层坐标**：只动位置，样式一律保持原值。

        - 位置 x/y 乘比例 → 弹幕铺满整个游戏窗口；
        - 字幕的 ``cx``（文字中心，用于居中绘制）也要跟着映射；
        - **字号 ``s``、文字矩形宽高 ``w/h``、颜色 ``c``、文本 ``t`` 一律不动**，
          这样字还是视频里的大小和样式（用户要求「字体除了位置以外其他样式都不变」）。
        """
        if abs(sx - 1.0) < 0.01 and abs(sy - 1.0) < 0.01:
            return items
        mapped = []
        for item in items:
            copy = dict(item)
            copy["x"] = int(round(float(item.get("x") or 0) * sx))
            copy["y"] = int(round(float(item.get("y") or 0) * sy))
            if item.get("cx") is not None:
                copy["cx"] = int(round(float(item["cx"]) * sx))
            mapped.append(copy)
        return mapped

    def _sync_and_draw(self) -> None:
        with self._lock:
            dirty = self._dirty
            visible = self._visible
            geometry = self._geometry
            items = list(self._items)
            subtitle = self._subtitle
            if dirty:
                self._dirty = False
                sx, sy = self._scale_for(geometry)
                blur = float((self._style or {}).get("blur") or 1.0)
                outline = max(1, min(MAX_OUTLINE, int(round(blur))))
                self._last_scale = (sx, sy)
                self._last_outline = outline
                self._frame = (geometry,
                               self._map_items(items, sx, sy),
                               self._map_items([subtitle], sx, sy)[0] if subtitle else None,
                               outline)
        if not dirty:
            return
        hwnd = self._hwnd
        if not hwnd:
            return
        x, y, width, height = geometry
        resized = geometry != self._drawn_geometry
        # 注意：这里**不能**传 HWND_TOPMOST —— 实测在已显示的窗口上再调用
        # SetWindowPos(HWND_TOPMOST) 反而会把置顶位弄丢（窗口被降级），
        # 置顶由创建时的 WS_EX_TOPMOST 保证，这里只移动/改尺寸。
        _user32.SetWindowPos(hwnd, None, x, y, width, height,
                             SWP_NOACTIVATE | SWP_NOZORDER
                             | (SWP_SHOWWINDOW if visible else 0))
        if resized:
            # 分层窗口的合成表面是按当时的尺寸建立的，改尺寸后要重新应用一次属性，
            # 否则只有旧尺寸那块区域能画出来。
            self._drawn_geometry = geometry
            _user32.SetLayeredWindowAttributes(hwnd, COLOR_KEY, 0, LWA_COLORKEY)
            self._release_buffer()
        if not visible:
            _user32.ShowWindow(hwnd, SW_HIDE)
            self._shown = False
            return
        if not self._shown:
            _user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            self._shown = True
        _user32.InvalidateRect(hwnd, None, False)
        _user32.UpdateWindow(hwnd)

    def _release_buffer(self) -> None:
        """释放复用的离屏位图（改尺寸 / 停止时）。"""
        cached = self._buffer
        self._buffer = None
        if not cached:
            return
        _mem_dc, bitmap = cached[2], cached[3]
        try:
            _gdi32.DeleteObject(bitmap)
            _gdi32.DeleteDC(_mem_dc)
        except Exception:
            pass

    def _acquire_buffer(self, target_dc, width: int, height: int):
        """拿一块复用的离屏 DC + 位图。

        覆盖层现在可能铺满 2K/4K 的游戏窗口，每帧重建 2560x1440 的位图太浪费
        （每帧十几 MB 的分配），所以按尺寸缓存复用。
        """
        cached = self._buffer
        if cached is not None and cached[0] == width and cached[1] == height:
            return cached[2], cached[3]
        self._release_buffer()
        mem_dc = _gdi32.CreateCompatibleDC(target_dc)
        bitmap = _gdi32.CreateCompatibleBitmap(target_dc, width, height)
        if not mem_dc or not bitmap:
            return None, None
        self._buffer = (width, height, mem_dc, bitmap)
        return mem_dc, bitmap

    def _draw(self, target_dc, width: int, height: int, items: list,
              subtitle: Optional[dict], outline: int = 1) -> None:
        self._last_draw = (width, height, len(items), bool(subtitle))
        if not target_dc:
            return
        started = time.perf_counter()
        mem_dc, bitmap = self._acquire_buffer(target_dc, width, height)
        if not mem_dc or not bitmap:
            return
        old_bitmap = _gdi32.SelectObject(mem_dc, bitmap)
        brush = _gdi32.CreateSolidBrush(COLOR_KEY)
        try:
            rect = wintypes.RECT(0, 0, width, height)
            _user32.FillRect(mem_dc, ctypes.byref(rect), brush)
            _gdi32.SetBkMode(mem_dc, TRANSPARENT)
            for item in items:
                self._draw_text(mem_dc, item, width, height, outline)
            if subtitle:
                self._draw_text(mem_dc, subtitle, width, height, outline)
            _gdi32.BitBlt(target_dc, 0, 0, width, height, mem_dc, 0, 0, SRCCOPY)
        finally:
            self._last_draw_ms = round((time.perf_counter() - started) * 1000, 2)
            _gdi32.SelectObject(mem_dc, old_bitmap)
            _gdi32.DeleteObject(brush)

    def _draw_text(self, mem_dc, item: dict, width: int, height: int, outline: int = 1) -> None:
        text = str(item.get("t") or "")
        if not text:
            return
        x = int(item.get("x") or 0)
        y = int(item.get("y") or 0)
        size = int(item.get("s") or 20)
        style = self._style or {}
        font = self._fonts.get(size, style.get("family") or "", style.get("bold", True))
        if not font:
            return
        _gdi32.SelectObject(mem_dc, font)
        flags = DT_SINGLELINE | DT_NOCLIP | DT_NOPREFIX
        if item.get("center"):
            # 字幕：页面里是居中排版的（而且外层容器比文字宽），
            # 所以先用 DT_CALCRECT 量出文字宽度，再按页面给的**文字中心**对齐画。
            measure = wintypes.RECT(0, 0, 0, 0)
            _user32.DrawTextW(mem_dc, text, -1, ctypes.byref(measure), flags | DT_CALCRECT)
            text_width = max(1, measure.right - measure.left)
            center = int(item.get("cx") or (x + text_width // 2))
            x = center - text_width // 2
            y = y + max(0, (int(item.get("h") or 0) - (measure.bottom - measure.top)) // 2)
        right = min(width + 2 * outline, x + 4000)
        # B 站是 0 偏移的 1px 黑晕，这里用同半径的圆盘偏移逐张画描边近似。
        # 描边用近黑（不是纯黑）：纯黑是颜色键，会被抠掉。
        _gdi32.SetTextColor(mem_dc, STROKE_COLOR)
        for dx, dy in _stroke_offsets(outline):
            _user32.DrawTextW(
                mem_dc, text, -1,
                ctypes.byref(wintypes.RECT(x + dx, y + dy, right, height)),
                flags,
            )
        _gdi32.SetTextColor(mem_dc, _nudge_color(item.get("c")))
        _user32.DrawTextW(
            mem_dc, text, -1,
            ctypes.byref(wintypes.RECT(x, y, right, height)),
            flags,
        )


# ---------------------------------------------------------------------------
# 主页面里的采集脚本：定时把当前「真实可见」的弹幕/字幕读出来推给子进程
# ---------------------------------------------------------------------------
# B 站实测：弹幕是 DOM（``.bili-danmaku-x-dm``），字幕也是 DOM
# （``.bili-subtitle-x-subtitle-panel-*``），都不是 canvas，所以直接读
# ``getBoundingClientRect()`` 就能拿到这一刻的动画位置，可以 1:1 镜像。
# 注意播放器会把弹幕节点「池化」复用，不可见的那些 opacity 是 0，
# 必须按 opacity 过滤，否则会有一堆东西堆在视频左上角。
MIRROR_JS = r"""
(function () {
    if (window.__okMirrorInstalled) { return true; }
    window.__okMirrorInstalled = true;

    var DM_SELECTORS = ['.bili-danmaku-x-dm', '.b-danmaku', '.bpx-player-dm-wrap > div'];
    var SUB_SELECTORS = [
        '.bili-subtitle-x-subtitle-panel-text',
        '.bpx-player-subtitle-panel-text',
        '.bili-subtitle-x-subtitle-panel-major-group .subtitle-item-text'
    ];
    var MAX_ITEMS = 120;
    var seq = 0, timer = null, enabled = false;

    function push(payload) {
        if (window.pywebview && window.pywebview.api && window.pywebview.api.ui) {
            try { window.pywebview.api.ui(payload); } catch (e) {}
        }
    }

    function visible(el) {
        var cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') { return false; }
        if (el.getClientRects().length === 0) { return false; }
        if (parseFloat(cs.opacity) <= 0.05) { return false; }
        // 必须真的落在**视口内**：实测 B 站播放器里存在「节点在、但画在视口外」的
        // 字幕容器（实测 y=501 而视口只有 361 高），不检查就会把看不见的东西镜像出来，
        // 表现为「字幕位置不对」（其实是被画到了屏幕外）。
        var r = el.getBoundingClientRect();
        if (r.right <= 0 || r.bottom <= 0 ||
            r.left >= window.innerWidth || r.top >= window.innerHeight) {
            return false;
        }
        return true;
    }

    function box(el) {
        var r = el.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) { return null; }
        var cs = getComputedStyle(el);
        return {
            x: Math.round(r.left), y: Math.round(r.top),
            w: Math.round(r.width), h: Math.round(r.height),
            s: Math.round(parseFloat(cs.fontSize) || 20),
            c: cs.color || 'rgb(255, 255, 255)'
        };
    }

    // 取站点自己的弹幕样式（字体 / 字重 / 描边宽度），保证画出来「和 B 站一致」。
    // 实测 B 站：SimHei, "Microsoft JhengHei", Arial... / 700 / text-shadow 0 0 1px 黑
    function readStyle(el) {
        if (!el) { return null; }
        var cs = getComputedStyle(el);
        var blur = 0;
        var parts = (cs.textShadow || '').split(',');
        for (var i = 0; i < parts.length; i++) {
            var nums = parts[i].match(/-?\d*\.?\d+px/g);
            if (!nums || !nums.length) { continue; }
            var value = Math.abs(parseFloat(nums[nums.length - 1]));
            if (value > blur) { blur = value; }
        }
        return {
            ff: cs.fontFamily || '',
            fw: cs.fontWeight || '',
            fs: Math.round(parseFloat(cs.fontSize) || 0),
            blur: blur
        };
    }

    // 量元素里**文字本身**的矩形。字幕的外层容器往往比文字宽（页面里是居中的），
    // 直接用容器矩形会让字幕画偏，所以用 Range 精确量文字。
    function textRect(el) {
        try {
            var range = document.createRange();
            range.selectNodeContents(el);
            var r = range.getBoundingClientRect();
            if (r && r.width >= 1 && r.height >= 1) { return r; }
        } catch (e) {}
        return el.getBoundingClientRect();
    }

    function subtitleBox(el) {
        var r = textRect(el);
        var cs = getComputedStyle(el);
        return {
            x: Math.round(r.left), y: Math.round(r.top),
            w: Math.round(r.width), h: Math.round(r.height),
            cx: Math.round(r.left + r.width / 2),
            s: Math.round(parseFloat(cs.fontSize) || 20),
            c: cs.color || 'rgb(255, 255, 255)',
            center: true
        };
    }

    function collect() {
        var nodes = [];
        for (var i = 0; i < DM_SELECTORS.length; i++) {
            var found = document.querySelectorAll(DM_SELECTORS[i]);
            if (found.length) { nodes = found; break; }
        }
        var items = [];
        var styleEl = null;
        for (var k = 0; k < nodes.length && items.length < MAX_ITEMS; k++) {
            var el = nodes[k];
            var text = (el.textContent || '').trim();
            if (!text || !visible(el)) { continue; }
            var b = box(el);
            if (!b) { continue; }
            if (!styleEl) { styleEl = el; }
            var id = el.getAttribute('data-ok-dm');
            if (!id) { id = 'dm' + (++seq); el.setAttribute('data-ok-dm', id); }
            b.i = id;
            b.t = text.slice(0, 120);
            items.push(b);
        }

        var sub = null;
        for (var m = 0; m < SUB_SELECTORS.length && !sub; m++) {
            var subs = document.querySelectorAll(SUB_SELECTORS[m]);
            for (var j = 0; j < subs.length; j++) {
                var el2 = subs[j];
                var text2 = (el2.textContent || '').trim();
                if (!text2 || !visible(el2)) { continue; }
                var b2 = subtitleBox(el2);
                if (!b2) { continue; }
                b2.t = text2.slice(0, 300);
                sub = b2;
                if (!styleEl) { styleEl = el2; }
                break;
            }
        }
        return {
            dm: items, sub: sub,
            vw: window.innerWidth, vh: window.innerHeight,
            style: readStyle(styleEl)
        };
    }

    function tick() {
        if (!enabled) { return; }
        try { push({ action: 'mirror', data: collect() }); } catch (e) {}
    }

    window.__okMirrorSet = function (on) {
        enabled = !!on;
        if (enabled) {
            if (timer == null) { timer = setInterval(tick, 60); }
            tick();
        } else if (timer != null) {
            clearInterval(timer);
            timer = null;
            push({ action: 'mirror', data: { dm: [], sub: null } });
        }
        return enabled;
    };
    window.__okMirrorActive = function () { return enabled; };
    return true;
})();
"""
