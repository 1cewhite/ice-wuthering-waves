"""弹幕数据链路（拉取解析 + 本地引擎）的离线回归测试。

这套测试**不依赖窗口**，所以跑得很快，可以随便跑：验的是「弹幕拿回来对不对」
和「按播放时刻算出来的位置对不对」。窗口相关的接线（覆盖层、游戏窗口锚点）
在 ``floating_browser_check.py`` 的 ``[7]`` 里。

联网那一节（真的去拉一段 B 站弹幕）默认也跑，但失败只记 WARN 不算失败
—— 接口不可用/没网不应该让回归变红。

用法::

    python tests/danmaku_engine_check.py
"""

from __future__ import annotations

import os
import random
import sys
import zlib

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from src.gui.floating_browser import bilibili_danmaku as bd  # noqa: E402
from src.gui.floating_browser.danmaku_engine import (  # noqa: E402
    DanmakuEngine, FIXED_DURATION, SCROLL_DURATION, estimate_width, max_rows)

FAILURES = 0
PASSED = 0


def check(ok: bool, label: str) -> None:
    global FAILURES, PASSED
    print(f"  {'[OK]' if ok else '[FAIL]'} {label}")
    if ok:
        PASSED += 1
    else:
        FAILURES += 1


def warn(label: str) -> None:
    print(f"  [WARN] {label}")


# ---------------------------------------------------------------- protobuf 构造


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        piece = value & 0x7F
        value >>= 7
        out.append(piece | 0x80 if value else piece)
        if not value:
            return bytes(out)


def _field(no: int, wire: int, payload: bytes) -> bytes:
    return _varint((no << 3) | wire) + payload


def _vfield(no: int, value: int) -> bytes:
    return _field(no, 0, _varint(value))


def _bfield(no: int, data: bytes) -> bytes:
    return _field(no, 2, _varint(len(data)) + data)


def make_segment(rows: list[tuple]) -> bytes:
    """按 B 站 DanmakuElem 的真实字段编号手搓一段 seg.so 响应。"""
    out = b""
    for index, (ms, mode, size, color, text, weight) in enumerate(rows):
        body = (_vfield(1, 12345 + index) + _vfield(2, ms) + _vfield(3, mode)
                + _vfield(4, size) + _vfield(5, color)
                + _bfield(7, text.encode("utf-8")) + _vfield(9, weight))
        out += _bfield(1, body)
    return out


# ---------------------------------------------------------------- 解析


def check_parse() -> None:
    print("[1] protobuf 解析（字段编号照 B 站真实格式）")
    raw = make_segment([
        (1500, 1, 25, 0xFFFFFF, "滚动弹幕", 9),
        (2000, 5, 25, 0xFF0000, "顶部弹幕", 5),
        (2500, 4, 18, 0x00FF00, "底部弹幕", 3),
    ])
    items = bd.parse_segment(raw)
    check(len(items) == 3, f"解出 3 条（实际 {len(items)}）")
    check(items[0].get("ms") == 1500 and items[0].get("mode") == 1, f"时间/模式: {items[0]}")
    check(items[0].get("text") == "滚动弹幕", f"文本: {items[0].get('text')!r}")
    check(items[0].get("color") == 0xFFFFFF and items[0].get("size") == 25,
          f"颜色/字号: {items[0].get('color'):#x} / {items[0].get('size')}")
    check(items[2].get("size") == 18, f"小字号档 18 也能读到（{items[2].get('size')}）")

    print("\n[2] XML 解析（含实体转义 / deflate 压缩）")
    xml = ('<?xml version="1.0" encoding="UTF-8"?><i><chatserver>chat</chatserver>'
           '<d p="0.60100,4,25,15138834,1604742895,0,9e5adeaa,40685126698926083,10">'
           'A &amp; B &lt;C&gt;</d>'
           '<d p="9.416,1,25,16777215,1611378869,0,14f71d3,44164288244350983">普通弹幕</d>'
           '</i>')
    items = bd.parse_xml(xml.encode("utf-8"))
    check(len(items) == 2, f"解出 2 条（实际 {len(items)}）")
    check(items[0]["t0"] == 0.601 and items[0]["text"] == "A & B <C>",
          f"时间 + 实体还原: t0={items[0]['t0']} text={items[0]['text']!r}")
    check(items[1]["t0"] == 9.416 and items[1]["color"] == 16777215,
          f"第二条: t0={items[1]['t0']} color={items[1]['color']}")
    packed = bd.parse_xml(zlib.compress(xml.encode("utf-8")))
    check(len(packed) == 2, f"deflate 压缩的 XML 也能解（{len(packed)} 条）")

    print("\n[3] 规范化：过滤不支持的模式、按时间排序")
    raw = make_segment([
        (5000, 1, 25, 0xFFFFFF, "后面的", 0),
        (1000, 7, 25, 0xFFFFFF, "高级弹幕（暂不支持）", 0),
        (2000, 1, 25, 0xFFFFFF, "前面的", 0),
        (3000, 8, 25, 0xFFFFFF, "代码弹幕（暂不支持）", 0),
        (4000, 1, 25, 0xFFFFFF, "", 0),
    ])
    normalized = bd._normalize(bd.parse_segment(raw))
    check([it["text"] for it in normalized] == ["前面的", "后面的"],
          f"丢掉 mode 7/8 与空文本，并排序 -> {[it['text'] for it in normalized]}")
    check(all(it["mode"] in bd.SUPPORTED_MODES for it in normalized), "只保留支持的模式")

    print("\n[4] 无效输入不炸")
    check(bd.parse_segment(b"") == [], "空字节 -> 空列表")
    check(bd.parse_xml(b"") == [], "空 XML -> 空列表")
    check(bd.parse_segment(b"\xff\xff\xff\xff") == [] or True, "垃圾字节不抛异常")
    check(bd.fetch(0) is None, "cid=0 -> None（调用方据此回退）")


# ---------------------------------------------------------------- 引擎


def check_engine_basic() -> None:
    width, height = 800, 400
    print("\n[5] 时间轴与轨迹（固定时长穿屏）")
    engine = DanmakuEngine()
    engine.load([{"t0": 5.0, "mode": 1, "size": 25, "color": 0xFFFFFF, "text": "五秒后"}])
    check(engine.frame(0.0, width, height) == [], "还没到时间 -> 空")
    frame = engine.frame(5.0, width, height)
    check(len(frame) == 1 and frame[0]["t"] == "五秒后", "到点出现")

    engine = DanmakuEngine()
    engine.load([{"t0": 0.0, "mode": 1, "size": 25, "color": 0xFFFFFF, "text": "轨迹测试"}])
    text_width = estimate_width("轨迹测试", 20)
    xs = []
    for step in range(80):
        probe = engine.frame(step * 0.1, width, height)
        xs.append(probe[0]["x"] if probe else None)
    check(xs[0] == width, f"t=0 从左边缘外进入（x={xs[0]} = {width}）")
    check(all(a is None or b is None or a >= b for a, b in zip(xs, xs[1:])), "x 单调左移")
    ok_formula = True
    for probe_t in (1.0, 3.0, 5.0, 7.0):
        got = engine.frame(probe_t, width, height)
        expect = width - (width + text_width) * probe_t / SCROLL_DURATION
        if not got or abs(got[0]["x"] - expect) > 1.5:
            ok_formula = False
    check(ok_formula, f"位置符合 x = W - (W+文本宽)·t/T（T={SCROLL_DURATION}s）")
    check(engine.frame(SCROLL_DURATION + 0.1, width, height) == [], "超时后消失")

    engine = DanmakuEngine()
    engine.load([{"t0": 0.0, "mode": 1, "size": 25, "color": 0xFFFFFF, "text": "短"},
                 {"t0": 0.0, "mode": 1, "size": 25, "color": 0xFFFFFF,
                  "text": "很长很长很长的一条弹幕文本"}])
    frame = {it["t"]: it for it in engine.frame(3.0, width, height)}
    check(frame["短"]["x"] > frame["很长很长很长的一条弹幕文本"]["x"],
          "固定时长（不是固定速度）：宽弹幕速度更快，所以落后得更多")


def check_engine_rows() -> None:
    width, height = 800, 400
    print("\n[6] 行分配避让：全速模拟 30 秒，同一行不得重叠")
    random.seed(7)
    words = ["666", "哈哈哈哈哈", "前方高能", "打卡", "awsl", "这个也太强了吧", "2333", "第一次看"]
    items, moment = [], 0.0
    while moment < 30.0:
        items.append({"t0": round(moment, 3), "mode": 1, "size": 25,
                      "color": 0xFFFFFF, "text": random.choice(words)})
        moment += random.uniform(0.05, 0.35)
    engine = DanmakuEngine()
    engine.load(items)
    overlaps = 0
    peak = 0
    for step in range(1800):
        frame = engine.frame(step / 60.0, width, height)
        peak = max(peak, len(frame))
        by_row: dict[int, list] = {}
        for it in frame:
            by_row.setdefault(it["y"], []).append(it)
        for row_items in by_row.values():
            if len(row_items) > 1:
                row_items.sort(key=lambda a: a["x"])
                for first, second in zip(row_items, row_items[1:]):
                    if first["x"] + estimate_width(first["t"], first["s"]) > second["x"] + 0.5:
                        overlaps += 1
    check(overlaps == 0, f"零重叠（同屏最多 {peak} 条）")
    check(engine.stats()["assigned"] >= len(items) * 0.5,
          f"大部分弹幕都排上了：{engine.stats()}")

    print("\n[7] 超密弹幕丢弃而不是叠在一起")
    engine = DanmakuEngine()
    engine.load([{"t0": i * 0.01, "mode": 1, "size": 25, "color": 0xFFFFFF, "text": "挤在一起"}
                 for i in range(400)])
    for step in range(120):
        engine.frame(step / 60.0, width, height)
    stats = engine.stats()
    check(stats["dropped"] > 0, f"确实丢弃了一部分（丢 {stats['dropped']} 条）")
    check(stats["rows"] == max_rows(height), f"行数 = 高度 ÷ 行高 = {stats['rows']}")

    print("\n[8] 顶部 / 底部弹幕居中、停留 4 秒、颜色取自弹幕本身")
    engine = DanmakuEngine()
    engine.load([{"t0": 0.0, "mode": 5, "size": 25, "color": 0xFF0000, "text": "顶部字幕"},
                 {"t0": 0.0, "mode": 4, "size": 25, "color": 0x00FF00, "text": "底部字幕"}])
    frame = engine.frame(0.01, width, height)
    check(len(frame) == 2, f"两条都在（{len(frame)}）")
    centered = all(
        abs(it["x"] - (width - estimate_width(it["t"], it["s"])) / 2) <= 1 for it in frame)
    check(centered, f"按各自文本宽度居中：x={[it['x'] for it in frame]}")
    check({it["c"] for it in frame} == {"rgb(255, 0, 0)", "rgb(0, 255, 0)"},
          f"颜色来自弹幕字段：{[it['c'] for it in frame]}")
    check(len(engine.frame(FIXED_DURATION - 0.1, width, height)) == 2, "3.9s 还在")
    check(engine.frame(FIXED_DURATION + 0.2, width, height) == [], f"{FIXED_DURATION}s 后消失")

    print("\n[9] 字号：弹幕自带的档位 × 0.8（B 站口径）")
    engine = DanmakuEngine()
    engine.load([{"t0": 0.0, "mode": 1, "size": 25, "color": 0xFFFFFF, "text": "标准"},
                 {"t0": 0.0, "mode": 1, "size": 18, "color": 0xFFFFFF, "text": "小号"}])
    frame = engine.frame(0.5, width, height)
    sizes = {it["t"]: it["s"] for it in frame}
    check(sizes.get("标准") == 20, f"25 档 -> {sizes.get('标准')}px")
    check(sizes.get("小号") == 14, f"18 档 -> {sizes.get('小号')}px")


def check_engine_seek() -> None:
    width, height = 800, 400
    print("\n[10] seek：往回拖要重放行状态，不能带着旧位置")
    engine = DanmakuEngine()
    engine.load([{"t0": round(i * 0.2, 3), "mode": 1, "size": 25, "color": 0xFFFFFF,
                  "text": f"第{i}条"} for i in range(100)])
    engine.frame(10.0, width, height)
    back = engine.frame(2.0, width, height)
    check(engine.stats()["rewinds"] >= 1, f"检测到 seek 并重放（rewinds={engine.stats()['rewinds']}）")
    check(len(back) > 0, f"往回拖后立刻有内容（{len(back)} 条）")
    # x 正好等于 width 是合法的（弹幕刚在右边缘进场）
    check(all(-300 < it["x"] <= width and 0 <= it["y"] < height for it in back),
          "重放后位置都在合理范围内")
    forward = engine.frame(9.0, width, height)
    check(len(forward) > 0, f"再往前拖也正常（{len(forward)} 条）")

    print("\n[11] 分辨率变化 / 极端尺寸")
    engine = DanmakuEngine()
    engine.load([{"t0": 0.0, "mode": 1, "size": 25, "color": 0xFFFFFF, "text": "尺寸"}])
    check(engine.frame(0.0, 0, 0) == [], "宽高为 0 -> 空")
    check(engine.frame(0.5, 40, 30) is not None, "极小尺寸不抛异常")
    engine.frame(0.6, 1600, 900)
    rows_big = engine.stats()["rows"]
    engine.frame(0.7, 800, 400)
    rows_small = engine.stats()["rows"]
    check(rows_big > rows_small, f"行数随高度变：{rows_small}（400 高）-> {rows_big}（900 高）")
    check(max_rows(1080) == 45, f"1080 高可排 {max_rows(1080)} 行")


def check_live() -> None:
    print("\n[12] 联网：真的去拉一段弹幕（失败只算 WARN）")
    try:
        cid = bd.resolve_cid("BV1GJ411x7h7", 1)
    except Exception as error:
        warn(f"解析 cid 失败: {error!r}")
        return
    if not cid:
        warn("解析 cid 返回 0（接口不可用？）")
        return
    check(cid == 137649199, f"bvid -> cid = {cid}")
    items = bd.fetch(cid)
    if not items:
        warn("拉取失败（可能是风控/断网），跳过联网断言")
        return
    check(len(items) > 500, f"拉到 {len(items)} 条（protobuf 通道通常比 XML 的 1200 上限更全）")
    check(all(items[i]["t0"] <= items[i + 1]["t0"] for i in range(len(items) - 1)), "已按时间排序")
    check(all(it["mode"] in bd.SUPPORTED_MODES for it in items), "模式都已规范化")
    engine = DanmakuEngine()
    engine.load(items)
    mid = items[len(items) // 2]["t0"] + 0.5
    frame = engine.frame(mid, 1600, 900)
    check(len(frame) > 0, f"用真实数据渲染 t={mid:.1f}s -> 同屏 {len(frame)} 条")
    check(engine.stats()["dropped"] < len(items) * 0.5,
          f"丢弃比例可接受：{engine.stats()['dropped']} / {len(items)}")


def main() -> int:
    print("== 弹幕数据链路离线检查 ==")
    check_parse()
    check_engine_basic()
    check_engine_rows()
    check_engine_seek()
    check_live()
    print(f"\n通过 {PASSED} / 失败 {FAILURES}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
