"""悬浮浏览器国际化的离线检查。

不依赖窗口，跑得很快：验的是「文案表、注入脚本、翻译目录三者是否对得上」——
这类问题（漏翻、key 写错、msgid 撞车）在界面上往往只表现为「某处显示了 key」，
不容易一眼看出来，所以用测试盯住。

用法::

    python tests/floating_browser_i18n.py
"""

from __future__ import annotations

import gettext
import json
import os
import re
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

FAILURES = 0
PASSED = 0
CJK = re.compile(r'[\u4e00-\u9fff]')
LOCALES = ('zh_CN', 'zh_TW', 'ja_JP', 'ko_KR', 'es_ES')


def check(ok: bool, label: str) -> None:
    global FAILURES, PASSED
    print(f"  {'[OK]' if ok else '[FAIL]'} {label}")
    if ok:
        PASSED += 1
    else:
        FAILURES += 1


def read(path: str) -> str:
    with open(path, encoding='utf-8') as handle:
        return handle.read()


# 先把 og.app 换成一个假的：真实 ok app 没在跑，而 og.app.tr 就是 gettext 入口
from ok import og  # noqa: E402


class _FakeApp:
    def __init__(self, locale: str) -> None:
        self._locale = locale
        self._t = gettext.translation('ok', os.path.join(PROJECT, 'i18n'), languages=[locale])

    def tr(self, key: str) -> str:
        return self._t.gettext(key) or key


from src.gui.floating_browser.i18n import (  # noqa: E402
    PAGE_TEXT, page_i18n_script, page_texts)

WEBVIEW = 'src/gui/floating_browser/webview_process.py'
TAB = 'src/gui/floating_browser/tab.py'
HOTKEYS = 'src/gui/floating_browser/hotkeys.py'


def check_binding() -> None:
    print("[1] 页面脚本用的 key 与文案表对得上")
    js = read(WEBVIEW)
    used = set(re.findall(r"T\('([^']+)'\)", js))
    defined = set(PAGE_TEXT)
    check(bool(used), f"页面脚本里确实在用 T(...)（{len(used)} 个 key）")
    missing = sorted(used - defined)
    check(not missing, f"脚本用到的 key 都在文案表里（缺: {missing}）")
    unused = sorted(defined - used)
    check(not unused, f"文案表里没有用不到的条key（多余: {unused}）")


def check_catalog() -> None:
    print("\n[2] 翻译目录覆盖了所有页面文案")
    for locale in LOCALES:
        po_path = f'i18n/{locale}/LC_MESSAGES/ok.po'
        mo_path = f'i18n/{locale}/LC_MESSAGES/ok.mo'
        check(os.path.exists(po_path), f"{locale}: ok.po 存在")
        check(os.path.exists(mo_path), f"{locale}: ok.mo 存在")
        if not os.path.exists(mo_path):
            continue
        translated = gettext.translation('ok', 'i18n', languages=[locale])
        missing = [english for english in PAGE_TEXT.values()
                   if not (translated.gettext(english) or '').strip()]
        check(not missing, f"{locale}: 全部页面文案都有译文（缺 {len(missing)} 条）")
        # 简体/繁体/日/韩 的译文不该等于英文原文（等于就是没翻）
        same = [english for english in PAGE_TEXT.values()
                if translated.gettext(english) == english]
        check(len(same) <= 1, f"{locale}: 译文不是照抄英文（照抄 {len(same)} 条）")


def check_page_texts() -> None:
    print("\n[3] page_texts() 随语言变化")
    og.app = _FakeApp('zh_CN')
    zh = page_texts()
    check(zh.get('panel.danmaku.title') == '弹幕设置',
          f"中文：{zh.get('panel.danmaku.title')!r}")
    check(all(value for value in zh.values()), "中文：没有空文案")

    og.app = _FakeApp('ja_JP')
    ja = page_texts()
    check(ja.get('panel.danmaku.title') == '弾幕設定',
          f"日文：{ja.get('panel.danmaku.title')!r}")
    check(ja.get('bar.drag') != zh.get('bar.drag'), "日文与中文不同")

    og.app = _FakeApp('es_ES')
    es = page_texts()
    check(es.get('panel.filter.scroll') == 'Desplazamiento',
          f"西班牙文：{es.get('panel.filter.scroll')!r}")

    # 没有 og.app 时要退回英文原文（不能崩，也不能返回空）
    og.app = None
    fallback = page_texts()
    check(fallback == PAGE_TEXT, "没有 ok app 时退回英文原文")
    check(fallback.get('panel.danmaku.title') == 'Danmaku settings',
          f"回退值就是英文原文：{fallback.get('panel.danmaku.title')!r}")


def check_script() -> None:
    print("\n[4] 注入脚本格式")
    script = page_i18n_script({'a.b': '中文值', 'c.d': 'quote " and \\ back'})
    check(script.startswith('(function(){'), "是自执行的函数体")
    check('__okI18N' in script and '__okT' in script, "挂了 __okI18N 与 __okT")
    match = re.search(r'window\.__okI18N = (\{.*?\});', script)
    check(bool(match), "文案表是 JSON 字面量")
    if match:
        try:
            parsed = json.loads(match.group(1))
            check(parsed.get('a.b') == '中文值', "JSON 可解析且内容正确")
        except ValueError as error:
            check(False, f"JSON 解析失败: {error}")


def check_sources() -> None:
    print("\n[5] 源码里的 msgid 都是英文")
    tab = read(TAB)
    chinese = [text for text in re.findall(r'self\.tr\(\s*"([^"]*)"', tab) if CJK.search(text)]
    check(not chinese, f"tab.py 的 self.tr 没有中文 msgid（残留 {chinese[:3]}）")

    hotkeys = read(HOTKEYS)
    labels = re.findall(r'"label":\s*"([^"]*)"', hotkeys)
    descriptions = re.findall(r'"description":\s*"([^"]*)"', hotkeys)
    bad = [text for text in labels + descriptions if CJK.search(text)]
    check(not bad, f"hotkeys.py 的动作名/说明都是英文（残留 {bad[:3]}）")
    check(len(labels) >= 8, f"热键动作都还在（{len(labels)} 个）")

    webview = read(WEBVIEW)
    # 工具条与设置面板的 JS 里不该再有中文文案（注释不算）
    stripped = re.sub(r'//[^\n]*', '', webview)
    blocks = re.findall(r'(CONTROL_BAR_JS|MIRROR_JS) = r"""(.*?)"""', stripped, re.S)
    leftovers = []
    for name, body in blocks:
        leftovers += [f"{name}:{text}" for text in re.findall(r'[\u4e00-\u9fff]+', body)]
    check(not leftovers, f"注入脚本里没有硬编码中文（残留 {leftovers[:5]}）")


def check_switch() -> None:
    print("\n[6] 切换语言后能立刻拿到新译文")
    for locale in ('zh_CN', 'zh_TW'):
        og.app = _FakeApp(locale)
        texts = page_texts()
        check(texts.get('panel.subtitle.title') in ('字幕设置', '字幕設定'),
              f"{locale}: 字幕设置 -> {texts.get('panel.subtitle.title')!r}")


def main() -> int:
    print("== 悬浮浏览器 i18n 检查 ==")
    check_binding()
    check_catalog()
    check_page_texts()
    check_script()
    check_sources()
    check_switch()
    print(f"\n通过 {PASSED} / 失败 {FAILURES}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
