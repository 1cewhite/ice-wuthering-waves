"""悬浮浏览器的国际化：把「注入页面里的文案」也接到 gettext 上。

为什么需要这个模块
------------------
ok 界面（``tab.py``）用 ``self.tr(...)``，走的是 ``og.app.tr`` -> gettext，
翻译条目统一放在 ``i18n/<locale>/LC_MESSAGES/ok.po``。

但悬浮窗的**工具条和设置面板是注入到网页里的 HTML/JS**，运行在 WebView2 子进程里，
拿不到 ``og``。所以这里的做法是：

1. 文案在 ``PAGE_TEXT`` 里集中登记（key -> 英文原文，英文原文就是 gettext 的 msgid）；
2. 主进程启动子进程前调用 :func:`page_texts`，用 gettext 翻译好整张表，
   放进启动配置传过去；
3. 子进程注入 ``window.__okI18N = {...}``，页面脚本用 ``T('bar.drag')`` 取值。

好处是**翻译仍然集中在 .po 文件里**，不会在 JS 里再维护一份语言表；
而且英文原文就是 msgid，和项目其它模块的做法一致（便于 ``gen_tr_po_files`` 收集）。

JS 侧如果拿不到表（比如注入失败），``T()`` 会退回 key 本身，
所以最坏情况是显示成 key，不会崩。
"""

from __future__ import annotations

import os
from typing import Any

# ---------------------------------------------------------------------------
# 页面文案表：key -> 英文原文（= gettext msgid）
# 只放「注入到网页里」的文案；ok 原生界面（tab.py）用 self.tr(...) 直接写英文。
# ---------------------------------------------------------------------------
PAGE_TEXT: dict[str, str] = {
    # 工具条
    "bar.drag": "Drag here to move the window",
    "bar.opacity": "Window opacity",
    "bar.click_through_short": "Click",
    "bar.click_through": "Click-through: clicks pass to the window below",
    "bar.danmaku": "Mirror danmaku onto the game (click to toggle, right-click for settings)",
    "bar.subtitle": "Mirror subtitles onto the game (click to toggle, right-click for settings; "
                    "enable CC in the player first)",
    "bar.on_top": "Toggle always-on-top",
    "bar.hide": "Hide the floating window (show it again from the ok window)",
    "bar.close": "Close the floating browser",
    # 设置面板 - 通用
    "panel.reset": "Reset",
    "panel.danmaku.title": "Danmaku settings",
    "panel.subtitle.title": "Subtitle settings",
    # 设置面板 - 弹幕
    "panel.filter": "Filter by type (checked = hidden)",
    "panel.filter.scroll": "Scrolling",
    "panel.filter.fixed": "Fixed",
    "panel.area": "Display area",
    "panel.opacity": "Opacity",
    "panel.font_size": "Font size",
    "panel.speed": "Speed",
    "panel.speed.very_slow": "Very slow",
    "panel.speed.slow": "Slow",
    "panel.speed.normal": "Normal",
    "panel.speed.fast": "Fast",
    "panel.speed.very_fast": "Very fast",
    "panel.danmaku.hint": "Options and steps follow the Bilibili player's danmaku settings.",
    # 设置面板 - 字幕
    "panel.subtitle.size": "Subtitle size",
    "panel.subtitle.position": "Subtitle position",
    "panel.subtitle.background": "Subtitle background opacity",
    "panel.subtitle.hint": "Larger value moves it lower (0 = top, 100 = bottom).",
    "panel.subtitle.note": "Subtitles can only be read from the page (the API needs login), "
                           "so turn on CC in the player first.",
}


def translate(english: str) -> str:
    """把一个英文原文翻成当前语言（走 ok 的 gettext，拿不到就原样返回）。"""
    if not english:
        return english
    try:
        from ok import og

        app = getattr(og, "app", None)
        if app is not None and hasattr(app, "tr"):
            return app.tr(english) or english
    except Exception:
        pass
    return english


def page_texts() -> dict[str, str]:
    """翻译整张页面文案表，交给子进程注入页面。

    注意：**必须整张表都翻译**（哪怕某条没有译文也要带上英文原文），
    否则 JS 侧 ``T(key)`` 取不到值就只能显示 key。
    """
    return {key: translate(english) for key, english in PAGE_TEXT.items()}


def page_i18n_script(texts: dict[str, str] | None = None) -> str:
    """生成注入用的 JS：把文案表挂到 ``window.__okI18N``。"""
    import json

    table: dict[str, Any] = dict(texts) if texts else page_texts()
    payload = json.dumps(table, ensure_ascii=False)
    return (
        "(function(){"
        f"window.__okI18N = {payload};"
        "if(typeof window.__okT !== 'function'){"
        "window.__okT = function(key, fallback){"
        "var table = window.__okI18N || {};"
        "var value = table[key];"
        "return (value === undefined || value === null || value === '') "
        "? (fallback === undefined ? key : fallback) : value;"
        "};}"
        "return true;})()"
    )


def current_locale() -> str:
    """当前语言代码（相对 ok 的 i18n 目录名，例如 ``zh_CN``）。"""
    try:
        from ok import og

        locale = getattr(og, "locale", None)
        if locale is not None and hasattr(locale, "name"):
            return str(locale.name())
    except Exception:
        pass
    return os.environ.get("LANG", "") or "unknown"
