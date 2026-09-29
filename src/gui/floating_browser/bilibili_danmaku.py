"""B 站弹幕资源拉取与解析（**不依赖页面 DOM**）。

为什么要自己拉
--------------
早先的做法是「读页面里弹幕元素的坐标再照搬到覆盖层」——本质是在抄上游的渲染结果，
于是被上游卡住：页面每 60ms 才更新一次坐标（要自己估速度、外推补帧）、
只能抄到「当前可见的那几条」、seek/倍速会让外推飘掉。
直接拿弹幕数据自己画就没有这些问题：位置由播放进度算出来，一条不少，随时可 seek。

数据源（2026-09-29 实测）
------------------------
1. **protobuf（主）**::

       https://api.bilibili.com/x/v2/dm/web/seg.so?type=1&oid=<cid>&segment_index=<n>

   官方播放器用的接口。每段 360 秒、最多约 6000 条，**不需要 cookie**。
   实测一个 1410 条弹幕的视频：一段拿全，平均 439ms。
   超出视频范围的段返回 **HTTP 304 + 空 body**（这就是停止条件，不是 404）。

2. **XML（回退）**::

       https://comment.bilibili.com/<cid>.xml

   结构简单但**有硬上限**（实测反复请求都稳定截断在 1200 条），只在 protobuf 失败时兜底。

字幕**不在这里**：``x/player/v2`` 的 ``subtitle_url`` 需要登录态，未登录时
``subtitles`` 一律为空（实测 12 个热门视频全是 0 条）。所以字幕只能继续走页面采集。

实测运动参数（供 ``danmaku_engine`` 使用）
----------------------------------------
- 滚动弹幕是**固定时长**穿屏（不是固定速度）：短弹幕 103px/s、长弹幕 117px/s，
  但 ``(播放器宽 + 文本宽) / 速度`` 都是约 7.1s。
- 行高 ≈ 字号 × 1.2（20px 字号 → 24px 行距）。
- 播放器显示字号 = 本模块的 ``size`` × 0.8（25 档 → 20px，18 档 → 14.4px）。
- 弹幕活动范围是**播放器宽度**，不是浏览器窗口宽度。
"""

from __future__ import annotations

import html
import re
import threading
import time
import zlib
from collections import OrderedDict
from typing import Any, Optional

from . import log as fb_log

# ---------------------------------------------------------------- 常量

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 每段的时长（秒），B 站固定 360 秒一段
SEGMENT_SECONDS = 360
# 最多拉多少段（兜底，防止异常 cid 导致死循环：40 段 = 4 小时）
MAX_SEGMENTS = 40
# 连续请求之间的间隔，避免触发风控
REQUEST_GAP = 0.12
# 单次请求超时（秒）
REQUEST_TIMEOUT = 10.0

# 弹幕模式（p 属性第 2 个字段 / protobuf 的 mode）
MODE_SCROLL = (1, 2, 3)   # 从右向左滚动
MODE_BOTTOM = 4           # 底部固定
MODE_TOP = 5              # 顶部固定
MODE_REVERSE = 6          # 从左向右（逆向）滚动
MODE_ADVANCED = 7         # 高级弹幕（JSON 指令），暂不支持
MODE_CODE = 8             # 代码弹幕，暂不支持
SUPPORTED_MODES = set(MODE_SCROLL) | {MODE_BOTTOM, MODE_TOP, MODE_REVERSE}


# ---------------------------------------------------------------- protobuf 解析
# 只按 wire format 解出需要的字段，不引入 protobuf 依赖。
#   message DmSegMobileReply { repeated DanmakuElem elems = 1; }
#   message DanmakuElem {
#     int64  id       = 1;
#     int32  progress = 2;   // 出现时间，毫秒
#     int32  mode     = 3;
#     int32  fontsize = 4;
#     uint32 color    = 5;   // RGB
#     string midHash  = 6;
#     string content  = 7;
#     int64  ctime    = 8;
#     int32  weight   = 9;
#     ...
#   }


def _varint(buf: bytes, index: int) -> tuple[int, int]:
    value = shift = 0
    size = len(buf)
    while index < size:
        byte = buf[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, index
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")
    # 数据被截断（响应不完整 / 不是 protobuf）：交给调用方当解析失败处理
    raise ValueError("varint 被截断")


def _read_fields(buf: bytes) -> list[tuple[int, int, Any]]:
    """极简 protobuf 字段读取：返回 ``[(字段号, wire_type, 值)]``。"""
    index = 0
    out: list[tuple[int, int, Any]] = []
    size = len(buf)
    while index < size:
        key, index = _varint(buf, index)
        field_no, wire = key >> 3, key & 7
        if wire == 0:                       # varint
            value, index = _varint(buf, index)
        elif wire == 2:                     # length-delimited
            length, index = _varint(buf, index)
            value = buf[index:index + length]
            index += length
        elif wire == 5:                     # 32-bit
            value = buf[index:index + 4]
            index += 4
        elif wire == 1:                     # 64-bit
            value = buf[index:index + 8]
            index += 8
        else:
            raise ValueError(f"不支持的 wire type {wire}")
        out.append((field_no, wire, value))
    return out


def parse_segment(raw: bytes) -> list[dict]:
    """把一段 seg.so 的响应解析成弹幕列表。

    对**非法/截断**的字节是容错的：解析到哪儿算哪儿，绝不抛异常
    （上游返回半截数据时不应该把整条链路带崩）。
    """
    try:
        top_fields = _read_fields(raw)
    except Exception:
        return []
    items: list[dict] = []
    for field_no, wire, value in top_fields:
        if field_no != 1 or wire != 2:
            continue
        try:
            sub_fields = _read_fields(value)
        except Exception:
            continue
        elem: dict[str, Any] = {}
        for sub_no, sub_wire, sub_value in sub_fields:
            if sub_no == 2:
                elem["ms"] = sub_value
            elif sub_no == 3:
                elem["mode"] = sub_value
            elif sub_no == 4:
                elem["size"] = sub_value
            elif sub_no == 5:
                elem["color"] = sub_value
            elif sub_no == 7:
                elem["text"] = sub_value.decode("utf-8", "replace")
            elif sub_no == 9:
                elem["weight"] = sub_value
        if elem.get("text") is not None:
            items.append(elem)
    return items


# ---------------------------------------------------------------- XML 回退

_XML_ITEM = re.compile(r'<d p="([^"]*)"[^>]*>(.*?)</d>', re.S)


def parse_xml(raw: bytes) -> list[dict]:
    """解析 XML 弹幕（``p`` 属性：时间,模式,字号,颜色,时间戳,池,用户,行ID[,权重]）。"""
    text = raw
    if not text.lstrip()[:1] == b"<":
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                text = zlib.decompress(raw, wbits)
                break
            except Exception:
                continue
    body = text.decode("utf-8", "replace")
    items: list[dict] = []
    for attr, content in _XML_ITEM.findall(body):
        parts = attr.split(",")
        if len(parts) < 4:
            continue
        try:
            items.append({
                "t0": float(parts[0]),
                "mode": int(parts[1]),
                "size": int(parts[2]),
                "color": int(parts[3]),
                "text": html.unescape(content),
                "weight": int(parts[8]) if len(parts) > 8 else 0,
            })
        except (ValueError, IndexError):
            continue
    return items


# ---------------------------------------------------------------- 规范化


def _normalize(items: list[dict]) -> list[dict]:
    """统一成引擎要的形状并按时间排序。

    统一字段：``t0``（秒）、``mode``、``size``、``color``（RGB 整数）、``text``、``weight``。
    """
    out: list[dict] = []
    for item in items:
        text = (item.get("text") or "").replace("\r", " ").replace("\n", " ").strip()
        if not text:
            continue
        if "t0" in item:
            t0 = float(item["t0"])
        else:
            t0 = float(item.get("ms") or 0) / 1000.0
        mode = int(item.get("mode") or 1)
        if mode not in SUPPORTED_MODES:
            continue
        out.append({
            "t0": round(t0, 3),
            "mode": mode,
            "size": int(item.get("size") or 25),
            "color": int(item.get("color") or 0xFFFFFF),
            "text": text[:120],
            "weight": int(item.get("weight") or 0),
        })
    out.sort(key=lambda it: it["t0"])
    return out


# ---------------------------------------------------------------- 网络


def _session():
    """延迟导入 requests 并建一个复用的会话（会话能省掉重复握手）。"""
    import requests

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
    })
    return session


def fetch_protobuf(cid: int, session, timeout: float = REQUEST_TIMEOUT) -> Optional[list[dict]]:
    """分段拉取 protobuf 弹幕；整段失败返回 ``None``（调用方可以回退 XML）。"""
    url = "https://api.bilibili.com/x/v2/dm/web/seg.so"
    collected: list[dict] = []
    for index in range(1, MAX_SEGMENTS + 1):
        try:
            resp = session.get(url, params={"type": 1, "oid": cid, "segment_index": index},
                               timeout=timeout)
        except Exception as error:
            fb_log.warning(f"弹幕段 {index} 请求失败: {error}")
            return collected or None
        # 超出视频范围的段：304 + 空 body（也见过 200 + 空）
        raw = resp.content
        if resp.status_code == 304 or not raw:
            break
        if raw[:1] == b"{":                 # 返回 JSON = 被风控或参数不对
            fb_log.warning(f"弹幕段 {index} 返回 JSON（可能被风控）: {raw[:120]!r}")
            return collected or None
        try:
            items = parse_segment(raw)
        except Exception as error:
            fb_log.warning(f"弹幕段 {index} 解析失败: {error}")
            break
        if not items:
            break
        collected.extend(items)
        fb_log.debug(f"弹幕段 {index}: {len(items)} 条（累计 {len(collected)}）")
        if len(items) < 10:                 # 最后一段通常很短
            break
        time.sleep(REQUEST_GAP)
    return collected or None


def fetch_xml(cid: int, session, timeout: float = REQUEST_TIMEOUT) -> Optional[list[dict]]:
    """XML 回退通道（注意有 1200 条左右的硬上限）。"""
    try:
        resp = session.get(f"https://comment.bilibili.com/{cid}.xml", timeout=timeout)
        items = parse_xml(resp.content)
    except Exception as error:
        fb_log.warning(f"XML 弹幕拉取失败: {error}")
        return None
    fb_log.info(f"XML 通道拿到 {len(items)} 条弹幕（该通道有条数上限）")
    return items or None


# ---------------------------------------------------------------- 缓存


class DanmakuCache:
    """按 cid 缓存已拉取的弹幕（LRU，线程安全）。"""

    def __init__(self, capacity: int = 8) -> None:
        self._lock = threading.RLock()
        self._items: "OrderedDict[int, list[dict]]" = OrderedDict()
        self._capacity = max(1, int(capacity))

    def get(self, cid: int) -> Optional[list[dict]]:
        with self._lock:
            items = self._items.get(int(cid))
            if items is None:
                return None
            self._items.move_to_end(int(cid))
            return items

    def put(self, cid: int, items: list[dict]) -> None:
        with self._lock:
            self._items[int(cid)] = items
            self._items.move_to_end(int(cid))
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_CACHE = DanmakuCache()


def resolve_cid(bvid: str, page: int = 1) -> int:
    """用 bvid + 分P 序号查出 cid（页面自己给不出 cid 时的兜底）。

    页面里的 ``__INITIAL_STATE__`` 一般直接带 cid；但 B 站改版 / 番剧页可能取不到，
    这时就把 bvid 丢过来查一次。
    """
    bvid = (bvid or "").strip()
    if not bvid:
        return 0
    try:
        session = _session()
        resp = session.get("https://api.bilibili.com/x/web-interface/view",
                           params={"bvid": bvid}, timeout=REQUEST_TIMEOUT)
        payload = resp.json() or {}
        data = payload.get("data") or {}
        pages = data.get("pages") or []
        if pages:
            index = max(1, int(page or 1)) - 1
            row = pages[index] if index < len(pages) else pages[0]
            cid = int(row.get("cid") or 0)
            if cid:
                fb_log.info(f"bvid={bvid} p={page} -> cid={cid}")
                return cid
        cid = int(data.get("cid") or 0)
        if cid:
            fb_log.info(f"bvid={bvid} -> cid={cid}（单 P）")
        return cid
    except Exception as error:
        fb_log.warning(f"解析 cid 失败 (bvid={bvid}): {error}")
        return 0


def fetch(cid: int, timeout: float = REQUEST_TIMEOUT) -> Optional[list[dict]]:
    """拉取某个 cid 的全量弹幕（带缓存）。

    返回 ``[{"t0": float, "mode": int, "size": int, "color": int, "text": str}, ...]``，
    按时间排序；拿不到任何数据时返回 ``None``（调用方应回退到页面采集）。
    """
    if not cid:
        return None
    cached = _CACHE.get(cid)
    if cached is not None:
        return cached

    started = time.perf_counter()
    try:
        session = _session()
    except Exception as error:
        fb_log.warning(f"requests 不可用，弹幕拉取放弃: {error}")
        return None

    items = fetch_protobuf(cid, session, timeout)
    source = "protobuf"
    if not items:
        items = fetch_xml(cid, session, timeout)
        source = "xml"
    if not items:
        fb_log.warning(f"cid={cid} 拿不到弹幕（两个通道都失败）")
        return None

    normalized = _normalize(items)
    if not normalized:
        fb_log.warning(f"cid={cid} 弹幕全部被过滤（不支持的模式）")
        return None
    _CACHE.put(cid, normalized)
    fb_log.info(
        f"cid={cid} 弹幕就绪: {len(normalized)} 条 / 通道={source} / "
        f"耗时 {(time.perf_counter() - started) * 1000:.0f}ms"
    )
    return normalized
