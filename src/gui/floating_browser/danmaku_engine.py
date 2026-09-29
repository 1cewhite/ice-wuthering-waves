"""本地弹幕引擎：把整段弹幕按播放进度算成「当前这一帧该画什么」。

和之前的 DOM 映射相比，这里的位置**不是抄来的，是算出来的**：
输入只有一个播放时刻 ``t_now``，输出是这一帧每条弹幕的 ``(x, y, 字号, 颜色, 文本)``。
所以 seek / 倍速 / 暂停天然正确，也不存在「采样间隔导致的跳帧」——
想跑多少帧率就跑多少帧率。

实测参数（2026-09-29，在真实 B 站播放器上采样得到的，不是拍的）
--------------------------------------------------------------
- **滚动弹幕是固定时长穿屏**，约 7.1 秒。这一点很容易搞错：它**不是**固定速度——
  实测短弹幕 103px/s、长弹幕 117px/s，但 ``(播放器宽 + 文本宽) / 速度`` 都约 7.1s。
  固定速度的实现会在长弹幕上明显偏慢。
- 行高 ≈ 字号 × 1.2（20px 字号 → 24px 行距，实测行间距中位数 24px）。
- 播放器显示字号 = 弹幕自带的 ``size`` × 0.8（standard 档 25 → 20px，小档 18 → 14.4px）。
- 顶部 / 底部弹幕停留约 4 秒。

坐标系
------
``x`` 是弹幕**左边缘**，屏幕左边缘为 0、右边缘为 ``width``。滚动弹幕从 ``x = width``
（右边缘外）进入，走到 ``x = -文本宽``（完全离开左边缘）为止。

行分配（避让）
--------------
每条弹幕进来时挑一行，要求：
1. 该行没有被固定弹幕（顶部/底部）占着；
2. 和该行上一条滚动弹幕**不会追尾**——注意宽度大的弹幕速度也大（固定时长），
   所以只检查「上一条是否已完全进入」是不够的，还要检查两条弹幕同时存在的
   整个区间内间距都不会变负。间距是时间的线性函数，取两个端点判断即可。
找不到行就丢弃该条（和 B 站播放器一样，宁可少一条也不要叠在一起）。
"""

from __future__ import annotations

import bisect
import unicodedata
from typing import Any, Callable, Optional

from . import log as fb_log
from .bilibili_danmaku import MODE_BOTTOM, MODE_REVERSE, MODE_SCROLL, MODE_TOP

# ---------------------------------------------------------------- 实测参数

# 滚动弹幕穿屏时长（秒）：从右边缘进、完全离开左边缘
SCROLL_DURATION = 7.1
# 顶部 / 底部弹幕停留时长（秒）
FIXED_DURATION = 4.0
# 行高 = 字号 × 该系数
LINE_RATIO = 1.2
# 播放器显示字号 = 弹幕 size × 该系数
FONT_SCALE = 0.8
# 字号上下限（防止异常数据画出怪东西）
MIN_FONT = 12
MAX_FONT = 64
# 行距基准字号：标准档 25 × 0.8 = 20px
BASE_FONT = round(25 * FONT_SCALE)

# 弹幕最长存活时间（用于 seek 后回退多少秒开始重放）
MAX_LIFE = max(SCROLL_DURATION, FIXED_DURATION)

# 相邻两条弹幕之间保留的最小间距（像素），避免贴在一起
MIN_GAP = 4.0

# 时间跳变超过这个量（秒）就认为发生了 seek / 跳转，重放行状态
SEEK_BACK_TOLERANCE = 0.05
SEEK_FORWARD_TOLERANCE = 2.0


def estimate_width(text: str, size: float) -> float:
    """在没有字体度量时的文本宽度估算（东亚全宽按 1em，拉丁按 0.5em）。"""
    total = 0.0
    for char in text:
        width_kind = unicodedata.east_asian_width(char)
        if width_kind in ("W", "F"):
            total += 1.0
        elif width_kind == "A":
            total += 0.6
        else:
            total += 0.5
    return total * size


Measure = Callable[[str, float], float]


class DanmakuEngine:
    """弹幕调度与位置计算。

    典型用法::

        engine = DanmakuEngine()
        engine.load(items)                 # 一次加载整段
        for frame in frames:
            engine.frame(t_now, width, height)   # -> [{"t":..,"x":..,"y":..,"s":..,"c":..}]
    """

    def __init__(self, measure: Optional[Measure] = None) -> None:
        self._measure: Measure = measure or estimate_width
        self._items: list[dict] = []
        self._times: list[float] = []
        self._cursor = 0
        self._active: list[dict] = []
        self._rows: list[dict] = []
        self._size_key: Optional[tuple[int, int]] = None
        self._last_t: Optional[float] = None
        # 诊断计数
        self.dropped = 0
        self.assigned = 0
        self.rewinds = 0

    # ------------------------------------------------------------ 载入

    def load(self, items: Optional[list[dict]]) -> None:
        """载入整段弹幕（``fetch()`` 的返回值），已按时间排序。"""
        self._items = list(items or [])
        self._items.sort(key=lambda it: it.get("t0") or 0.0)
        self._times = [float(it.get("t0") or 0.0) for it in self._items]
        self.dropped = 0
        self.assigned = 0
        self.rewinds = 0
        self.reset()

    def reset(self) -> None:
        """回到「还没开始播放」的状态。"""
        self._cursor = 0
        self._active = []
        self._last_t = None
        self._clear_rows()

    def set_measure(self, measure: Optional[Measure]) -> None:
        """注入精确的文本度量（例如覆盖层的 GDI 测量）；传 None 回到估算。"""
        self._measure = measure or estimate_width

    @property
    def loaded(self) -> int:
        return len(self._items)

    # ------------------------------------------------------------ 行

    def _clear_rows(self) -> None:
        for row in self._rows:
            row["scroll"] = None
            row["fixed_until"] = -1.0

    def _build_rows(self, width: int, height: int) -> None:
        """按覆盖层高度切行（行高固定，不随覆盖层大小缩放字号）。"""
        line_height = max(8, int(round(BASE_FONT * LINE_RATIO)))
        rows = []
        y = 0
        while y + line_height <= height:
            rows.append({"y": y, "line_height": line_height, "scroll": None, "fixed_until": -1.0})
            y += line_height
        self._rows = rows

    # ------------------------------------------------------------ 主循环

    def frame(self, t_now: float, width: int, height: int) -> list[dict]:
        """算出 ``t_now`` 时刻这一帧该画的弹幕。"""
        if not self._items or width <= 0 or height <= 0:
            return []

        if self._size_key != (width, height):
            self._size_key = (width, height)
            self._build_rows(width, height)
            self._rewind(t_now)
        elif self._last_t is None or self._needs_rewind(t_now):
            self._rewind(t_now)
        self._last_t = t_now

        self._advance(t_now, width, height)
        return self._collect(t_now, width)

    def _needs_rewind(self, t_now: float) -> bool:
        last = self._last_t
        if last is None:
            return True
        if t_now < last - SEEK_BACK_TOLERANCE:
            return True          # 往回拖了
        return t_now - last > SEEK_FORWARD_TOLERANCE

    def _rewind(self, t_now: float) -> None:
        """从 ``t_now - MAX_LIFE`` 开始重放（更早的弹幕已经不影响当前帧）。"""
        self.rewinds += 1
        self._active = []
        self._clear_rows()
        start = max(0.0, t_now - MAX_LIFE)
        self._cursor = bisect.bisect_left(self._times, start)

    def _advance(self, t_now: float, width: int, height: int) -> None:
        """把「已经该出现」的弹幕依次分配位置。"""
        items = self._items
        while self._cursor < len(items):
            item = items[self._cursor]
            if item["t0"] > t_now:
                break
            self._cursor += 1
            self._place(item, t_now, width, height)
        # 清掉已经离开的
        self._active = [entry for entry in self._active if t_now - entry["t0"] < entry["life"]]

    def _place(self, item: dict, t_now: float, width: int, height: int) -> None:
        mode = item["mode"]
        size = self._font_size(item)
        text_width = float(self._measure(item["text"], size))
        if mode in MODE_SCROLL:
            self._place_scroll(item, size, text_width, width)
        elif mode == MODE_REVERSE:
            self._place_reverse(item, size, text_width, width)
        else:
            self._place_fixed(item, size, text_width, width, bottom=(mode == MODE_BOTTOM))

    def _place_scroll(self, item: dict, size: float, text_width: float, width: int) -> None:
        distance = width + text_width
        speed = distance / SCROLL_DURATION
        row = self._find_row_for_scroll(item["t0"], width, text_width, speed)
        if row is None:
            self.dropped += 1
            return
        row["scroll"] = {"t0": item["t0"], "width": text_width, "speed": speed}
        self.assigned += 1
        self._active.append({
            "t0": item["t0"], "mode": item["mode"], "text": item["text"],
            "color": item["color"], "size": size, "width": text_width,
            "y": row["y"], "speed": speed, "direction": -1, "life": SCROLL_DURATION,
        })

    def _place_reverse(self, item: dict, size: float, text_width: float, width: int) -> None:
        distance = width + text_width
        speed = distance / SCROLL_DURATION
        row = self._find_row_for_scroll(item["t0"], width, text_width, speed, reverse=True)
        if row is None:
            self.dropped += 1
            return
        row["scroll"] = {"t0": item["t0"], "width": text_width, "speed": speed, "reverse": True}
        self.assigned += 1
        self._active.append({
            "t0": item["t0"], "mode": item["mode"], "text": item["text"],
            "color": item["color"], "size": size, "width": text_width,
            "y": row["y"], "speed": speed, "direction": 1, "life": SCROLL_DURATION,
        })

    def _place_fixed(self, item: dict, size: float, text_width: float, width: int,
                     bottom: bool) -> None:
        row = self._find_row_for_fixed(item["t0"], bottom)
        if row is None:
            self.dropped += 1
            return
        # 固定弹幕显示期间，该行谁也进不来
        row["fixed_until"] = item["t0"] + FIXED_DURATION
        row["scroll"] = None
        self.assigned += 1
        self._active.append({
            "t0": item["t0"], "mode": item["mode"], "text": item["text"],
            "color": item["color"], "size": size, "width": text_width,
            "y": row["y"], "speed": 0.0, "direction": 0, "life": FIXED_DURATION,
            "centered": True,
        })

    def _find_row_for_scroll(self, t0: float, width: int, text_width: float,
                             speed: float, reverse: bool = False) -> Optional[dict]:
        rows = self._rows
        order = range(len(rows) - 1, -1, -1) if reverse else range(len(rows))
        for index in order:
            row = rows[index]
            if row["fixed_until"] > t0:
                continue
            last = row["scroll"]
            if last is None:
                return row
            if self._can_share_row(last, t0, text_width, speed, width):
                return row
        return None

    @staticmethod
    def _can_share_row(last: dict, t0: float, text_width: float,
                       speed: float, width: int) -> bool:
        """判断新弹幕能否复用 ``last`` 所在的行（不追尾）。

        间距 ``d(t) = 新弹幕头部 - 上一条尾部``。两条都是匀速，

             d(t) = (上一条已走的距离) - 上一条宽度 - (新弹幕已走的距离)

        是 ``t`` 的线性函数，所以最小值必在一端：取「新弹幕刚出现」和
        「上一条即将离开左边缘」两个时刻分别检查即可。
        （宽度大的弹幕速度也大，所以不能只看「上一条是否已完全进入」。）
        """
        dt = t0 - last["t0"]
        if dt < 0:
            return False
        last_speed = last["speed"]
        last_width = last["width"]
        # t = t0：上一条已经走了 last_speed * dt
        gap_start = last_speed * dt - last_width
        # t = last["t0"] + SCROLL_DURATION：上一条尾部正好到左边缘
        remain = last["t0"] + SCROLL_DURATION - t0
        gap_end = width - speed * remain
        return min(gap_start, gap_end) >= MIN_GAP

    def _find_row_for_fixed(self, t0: float, bottom: bool) -> Optional[dict]:
        rows = self._rows
        order = range(len(rows) - 1, -1, -1) if bottom else range(len(rows))
        for index in order:
            row = rows[index]
            if row["fixed_until"] <= t0:
                return row
        return None

    # ------------------------------------------------------------ 输出

    def _collect(self, t_now: float, width: int) -> list[dict]:
        out: list[dict] = []
        for entry in self._active:
            age = t_now - entry["t0"]
            if age < 0 or age >= entry["life"]:
                continue
            text_width = entry["width"]
            if entry.get("centered"):
                x = (width - text_width) / 2.0
            elif entry["direction"] < 0:
                x = width - entry["speed"] * age
            else:
                x = -text_width + entry["speed"] * age
            if x > width or x + text_width < 0:
                continue
            out.append({
                "t": entry["text"],
                "x": int(round(x)),
                "y": int(entry["y"]),
                "s": int(round(entry["size"])),
                "c": _css_color(entry["color"]),
            })
        return out

    def _font_size(self, item: dict) -> float:
        size = float(item.get("size") or 25) * FONT_SCALE
        return float(max(MIN_FONT, min(MAX_FONT, round(size))))

    # ------------------------------------------------------------ 诊断

    def stats(self) -> dict:
        return {
            "loaded": len(self._items),
            "cursor": self._cursor,
            "active": len(self._active),
            "rows": len(self._rows),
            "assigned": self.assigned,
            "dropped": self.dropped,
            "rewinds": self.rewinds,
        }


def _css_color(value: Any) -> str:
    """protobuf / XML 里的 RGB 整数 -> CSS 颜色字符串。"""
    try:
        rgb = int(value)
    except (TypeError, ValueError):
        rgb = 0xFFFFFF
    rgb &= 0xFFFFFF
    return f"rgb({(rgb >> 16) & 0xFF}, {(rgb >> 8) & 0xFF}, {rgb & 0xFF})"


def max_rows(height: int) -> int:
    """给定高度最多能放几行（诊断用）。"""
    line_height = max(8, int(round(BASE_FONT * LINE_RATIO)))
    return max(0, height // line_height)
