"""字幕覆盖层：真正的逐像素半透明（``UpdateLayeredWindow`` + Pillow 渲染）。

为什么不和弹幕层共用一个窗口
----------------------------
弹幕层用的是「颜色键」抠图（``SetLayeredWindowAttributes(key=纯黑, LWA_COLORKEY)``），
而**颜色键只有「全透明 / 全不透明」两种状态**，做不了半透明背景 —— 但字幕设置里
就有「背景不透明度」。所以字幕单独开一个窗口，走真正的 per-pixel alpha：

- 用 Pillow 渲染成 RGBA 位图（Pillow 能直接吐出 ``BGRA`` 字节，不用手动换通道）；
- 写进 32bpp 的 DIB section（``biHeight`` 取负 = top-down，省一次翻转）；
- ``UpdateLayeredWindow(..., ULW_ALPHA)`` 把带 alpha 的位图贴到屏幕上。

这样背景可以是任意透明度，文字抗锯齿也由 FreeType 处理，比颜色键干净。

为什么开销不是问题
------------------
窗口只包住「字幕那一块」（宽 = 文字宽 + 内边距，高 = 字号 + 内边距），
每次重绘的数据量只有几十 KB；而且字幕内容变化很慢（页面每 200ms 上报一次，
内容变一次才需要重绘），不需要像弹幕那样每帧重画。
实测 Pillow 生成 1600×120 的位图 + 转 BGRA 只要 0.3ms 量级，完全够用。
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from typing import Any, Optional

from . import log as fb_log

_PIL_OK = True
try:  # 延迟到真正用的时候才需要 Pillow
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover - 没装 Pillow 时退化为「不显示字幕层」
    _PIL_OK = False
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Win32
# ---------------------------------------------------------------------------
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0

# 字幕窗口的四角圆角半径（背景块的观感，和常见播放器一致）
BACKGROUND_RADIUS = 4

_user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True) if os.name == "nt" else None

if _user32 is not None:
    # 句柄/指针一律 c_void_p：ok 框架把 GetModuleHandleW 的 restype 设成 32 位，
    # 不显式声明的话 64 位句柄会被静默截断（本项目踩过两次）。
    _user32.CreateWindowExW.argtypes = [
        ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _user32.CreateWindowExW.restype = ctypes.c_void_p
    _user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                       ctypes.c_void_p, ctypes.c_void_p]
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.DestroyWindow.argtypes = [ctypes.c_void_p]
    _user32.DestroyWindow.restype = ctypes.c_int
    _user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.ShowWindow.restype = ctypes.c_int
    _user32.GetDC.argtypes = [ctypes.c_void_p]
    _user32.GetDC.restype = ctypes.c_void_p
    _user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.ReleaseDC.restype = ctypes.c_int
    _user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                       ctypes.c_void_p, ctypes.c_void_p]
    _user32.UpdateLayeredWindow.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
    ]
    _user32.UpdateLayeredWindow.restype = ctypes.c_int
    _user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    _user32.RegisterClassW.restype = ctypes.c_ushort
    _user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    _user32.SetWindowPos.restype = ctypes.c_int
    _user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.GetWindowRect.restype = ctypes.c_int
    _user32.IsWindow.argtypes = [ctypes.c_void_p]
    _user32.IsWindow.restype = ctypes.c_int

    _gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    _gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    _gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    _gdi32.DeleteDC.restype = ctypes.c_int
    _gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _gdi32.SelectObject.restype = ctypes.c_void_p
    _gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    _gdi32.DeleteObject.restype = ctypes.c_int
    _gdi32.CreateDIBSection.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint,
    ]
    _gdi32.CreateDIBSection.restype = ctypes.c_void_p


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                             ctypes.c_void_p, ctypes.c_void_p)

# ---------------------------------------------------------------------------
# 字体 / 颜色
# ---------------------------------------------------------------------------
# 站点样式里给的是 CSS 字体名，Pillow 要字体文件
_FONT_FILES = {
    "simhei": "C:/Windows/Fonts/simhei.ttf",
    "microsoft yahei": "C:/Windows/Fonts/msyh.ttc",
    "微软雅黑": "C:/Windows/Fonts/msyh.ttc",
    "simsun": "C:/Windows/Fonts/simsun.ttc",
    "宋体": "C:/Windows/Fonts/simsun.ttc",
    "arial": "C:/Windows/Fonts/arial.ttf",
    "segoe ui": "C:/Windows/Fonts/segoeui.ttf",
}
_DEFAULT_FONT = "C:/Windows/Fonts/simhei.ttf"


def _font_path(family: str) -> str:
    name = (family or "").split(",")[0].strip().strip("\"'").lower()
    if name in _FONT_FILES and os.path.exists(_FONT_FILES[name]):
        return _FONT_FILES[name]
    for key, path in _FONT_FILES.items():
        if (key in name or name in key) and os.path.exists(path):
            return path
    return _DEFAULT_FONT


def _parse_color(value: Any) -> tuple[int, int, int]:
    """CSS 颜色字符串（``rgb(r, g, b)``）-> (r, g, b)。"""
    text = str(value or "").strip()
    if text.startswith("rgb"):
        parts = []
        for chunk in text[text.find("(") + 1: text.rfind(")")].split(","):
            try:
                parts.append(max(0, min(255, int(round(float(chunk.strip()))))))
            except ValueError:
                parts.append(0)
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
    return 255, 255, 255


class _FontCache:
    """按 (字号, 字体, 粗体) 缓存 Pillow 字体对象。"""

    def __init__(self) -> None:
        self._fonts: dict[tuple, Any] = {}

    def get(self, size: int, family: str = "", bold: bool = False):
        if not _PIL_OK:
            return None
        key = (int(size), (family or "").lower(), bool(bold))
        font = self._fonts.get(key)
        if font is not None:
            return font
        path = _font_path(family)
        try:
            font = ImageFont.truetype(path, int(size))
        except Exception as error:  # pragma: no cover
            fb_log.warning(f"字幕字体加载失败 {path}: {error}")
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
        self._fonts[key] = font
        return font

    def clear(self) -> None:
        self._fonts.clear()


# ---------------------------------------------------------------------------
# 覆盖层
# ---------------------------------------------------------------------------


class SubtitleOverlay:
    """字幕窗口：只包住字幕那一块，带真正的 alpha。

    线程模型和 ``DanmakuOverlay`` 一致：``start()`` 起一个线程建窗口 + 消息泵；
    外部只用 ``set_content`` / ``set_options`` / ``set_geometry`` / ``set_visible``。
    """

    CLASS_NAME = "OKWWSubtitleOverlayCanvas"
    # 字幕内容变化才重绘，轮询间隔不用太密
    IDLE_WAIT = 0.016

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._hwnd = 0
        self._lock = threading.RLock()
        self._dirty = True
        self._visible = False
        self._shown = False
        self._subtitle: Optional[dict] = None
        self._style: dict = {}
        self._options: dict = {"font_scale": 1.0, "position": 88, "bg_opacity": 0}
        self._geometry = (0, 0, 0, 0)
        self._fonts = _FontCache()
        self._last_error = ""
        self._last_draw = None      # (x, y, w, h, text)
        self._draw_ms = 0.0
        self._wndproc_ref = None    # 防 GC
        self._applied = None        # 上一次真正贴上去的内容签名

    # ------------------------------------------------------------------ 生命周期
    @property
    def hwnd(self) -> int:
        return self._hwnd

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if os.name != "nt" or _user32 is None:
            self._last_error = "非 Windows 平台"
            return False
        if not _PIL_OK:
            self._last_error = "Pillow 不可用"
            return False
        with self._lock:
            if self.running:
                return True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="SubtitleOverlay",
                                            daemon=True)
            self._thread.start()
        deadline = time.time() + 5.0
        while time.time() < deadline and not self._hwnd:
            time.sleep(0.05)
        return bool(self._hwnd)

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        self._fonts.clear()

    # ------------------------------------------------------------------ 对外接口
    def set_geometry(self, x: int, y: int, width: int, height: int) -> None:
        """字幕的作用区域（= 游戏画面），位置按它算。"""
        rect = (int(x), int(y), int(width), int(height))
        with self._lock:
            if rect == self._geometry:
                return
            self._geometry = rect
            self._dirty = True

    def set_content(self, subtitle: Optional[dict], style: Optional[dict] = None) -> None:
        """字幕内容（页面采集来的那条）。``None`` 表示当前没有字幕。"""
        with self._lock:
            new_subtitle = subtitle or None
            new_style = dict(style or {})
            if new_subtitle == self._subtitle and new_style == self._style:
                return
            self._subtitle = new_subtitle
            self._style = new_style
            self._dirty = True

    def set_options(self, settings: Optional[dict]) -> None:
        """字幕设置：``font_scale`` / ``position`` / ``bg_opacity``。"""
        settings = dict(settings or {})
        merged = dict(self._options)
        for key in ("font_scale", "position", "bg_opacity"):
            if settings.get(key) is not None:
                merged[key] = float(settings[key])
        merged["font_scale"] = max(0.5, min(2.0, merged["font_scale"]))
        merged["position"] = max(0.0, min(100.0, merged["position"]))
        merged["bg_opacity"] = max(0.0, min(100.0, merged["bg_opacity"]))
        with self._lock:
            if merged == self._options:
                return
            self._options = merged
            self._dirty = True

    def set_visible(self, visible: bool) -> None:
        with self._lock:
            self._visible = bool(visible)
            self._dirty = True

    def debug_info(self) -> dict:
        hwnd = self._hwnd
        info = {
            "hwnd": hwnd, "shown": self._shown, "options": dict(self._options),
            "geometry": self._geometry, "last_draw": self._last_draw,
            "draw_ms": round(self._draw_ms, 2), "has_subtitle": bool(self._subtitle),
            "pil": _PIL_OK, "last_error": self._last_error,
        }
        if hwnd and os.name == "nt":
            rect = wintypes.RECT()
            if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                info["rect"] = (rect.left, rect.top,
                                rect.right - rect.left, rect.bottom - rect.top)
        return info

    # ------------------------------------------------------------------ 线程
    def _run(self) -> None:
        user32 = _user32
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        hmod = kernel32.GetModuleHandleW(None)

        self._wndproc_ref = WNDPROC(self._on_message)
        wc = WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p).value
        wc.hInstance = hmod
        wc.lpszClassName = self.CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(wc)):
            pass  # 已注册过就继续
        # WS_EX_TOPMOST 必须在创建时带上：在已显示的窗口上再 SetWindowPos(HWND_TOPMOST)
        # 反而会把置顶位弄丢（弹幕层踩过这个坑）
        ex_style = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
                    | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
        hwnd = user32.CreateWindowExW(
            ex_style, self.CLASS_NAME, "subtitle-overlay", WS_POPUP,
            0, 0, 1, 1, None, None, hmod, None,
        )
        if not hwnd:
            self._last_error = f"CreateWindowExW 失败 err={ctypes.get_last_error()}"
            fb_log.warning(f"字幕层创建失败: {self._last_error}")
            return
        self._hwnd = int(hwnd)

        msg = wintypes.MSG()
        while not self._stop_event.is_set():
            try:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                self._tick()
            except Exception as error:
                self._last_error = repr(error)
                fb_log.warning(f"字幕层异常: {error!r}")
            self._stop_event.wait(self.IDLE_WAIT)

        try:
            user32.DestroyWindow(hwnd)
        except Exception:
            pass
        self._hwnd = 0
        self._shown = False

    def _on_message(self, hwnd, message, wparam, lparam):
        if message == 0x0002:  # WM_DESTROY
            return 0
        return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

    # ------------------------------------------------------------------ 渲染
    def _tick(self) -> None:
        with self._lock:
            dirty = self._dirty
            visible = self._visible
            subtitle = self._subtitle
            style = dict(self._style)
            options = dict(self._options)
            geometry = self._geometry
            self._dirty = False
        hwnd = self._hwnd
        if not hwnd:
            return
        if not dirty:
            return
        if not visible or not subtitle or not subtitle.get("t"):
            if self._shown:
                _user32.ShowWindow(hwnd, SW_HIDE)
                self._shown = False
                self._applied = None
            return
        self._render(hwnd, subtitle, style, options, geometry)

    def _render(self, hwnd, subtitle: dict, style: dict, options: dict,
                geometry: tuple) -> None:
        geo_x, geo_y, geo_w, geo_h = geometry
        if geo_w <= 0 or geo_h <= 0:
            return
        started = time.perf_counter()

        base_size = float(subtitle.get("s") or 20)
        font_size = int(max(10, min(160, round(base_size * options.get("font_scale", 1.0)))))
        family = style.get("family") or "SimHei"
        font = self._fonts.get(font_size, family, bool(style.get("bold", True)))
        text = str(subtitle.get("t") or "")
        if font is None or not text:
            return

        # 先量文字：窗口只包住文字 + 内边距，重绘的数据量才小
        probe = Image.new("RGBA", (1, 1))
        probe_draw = ImageDraw.Draw(probe)
        try:
            text_width = float(probe_draw.textlength(text, font=font))
        except Exception:
            text_width = float(len(text) * font_size)
        text_width = min(text_width, max(20.0, geo_w - 24.0))

        pad_x = max(6, int(round(font_size * 0.45)))
        pad_y = max(3, int(round(font_size * 0.22)))
        width = int(round(text_width)) + pad_x * 2
        height = font_size + pad_y * 2 + 2
        width = max(16, min(int(geo_w), width))

        background = int(round(255 * options.get("bg_opacity", 0) / 100.0))
        color = _parse_color(subtitle.get("c"))

        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        if background > 0:
            draw.rounded_rectangle([0, 0, width - 1, height - 1],
                                   radius=BACKGROUND_RADIUS, fill=(0, 0, 0, background))
        draw.text((pad_x, pad_y), text, font=font, fill=color + (255,),
                  stroke_width=1, stroke_fill=(0, 0, 0, 255))

        data = image.tobytes("raw", "BGRA")
        x = int(geo_x + (geo_w - width) / 2)
        y = int(geo_y + geo_h * options.get("position", 88) / 100.0 - height / 2)
        y = max(0, min(int(geo_y + geo_h - height), y))
        self._present(hwnd, x, y, width, height, data)
        self._last_draw = (x, y, width, height, text[:40])
        self._draw_ms = (time.perf_counter() - started) * 1000.0

    def _present(self, hwnd, x: int, y: int, width: int, height: int, data: bytes) -> None:
        """把带 alpha 的位图贴到分层窗口上（``ULW_ALPHA``）。"""
        screen_dc = _user32.GetDC(None)
        if not screen_dc:
            return
        mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
        bitmap = None
        try:
            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height     # 负高 = top-down，省一次上下翻转
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = BI_RGB
            bits = ctypes.c_void_p()
            bitmap = _gdi32.CreateDIBSection(
                screen_dc, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
            if not bitmap or not bits:
                return
            old = _gdi32.SelectObject(mem_dc, bitmap)
            ctypes.memmove(bits, data, min(len(data), width * height * 4))
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            size = wintypes.SIZE(int(width), int(height))
            dst = wintypes.POINT(int(x), int(y))
            src = wintypes.POINT(0, 0)
            _user32.UpdateLayeredWindow(
                hwnd, screen_dc, ctypes.byref(dst), ctypes.byref(size),
                mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
            _gdi32.SelectObject(mem_dc, old)
        finally:
            if bitmap:
                _gdi32.DeleteObject(bitmap)
            if mem_dc:
                _gdi32.DeleteDC(mem_dc)
            _user32.ReleaseDC(None, screen_dc)
        if not self._shown:
            _user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            self._shown = True
