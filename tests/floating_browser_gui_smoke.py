"""GUI 启动冒烟测试。

在真实的 Qt 应用中实例化悬浮浏览器标签页，验证：
- 标签页能正常构建（所有控件创建成功）
- 配置读写不报错
- 启动/停止悬浮浏览器链路可用（可选，用 --launch 开启）

用法：
    .\\.venv\\Scripts\\python.exe tests/floating_browser_gui_smoke.py
    .\\.venv\\Scripts\\python.exe tests/floating_browser_gui_smoke.py --launch
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)
os.chdir(PROJECT)


def main() -> int:
    parser = argparse.ArgumentParser(description="悬浮浏览器 GUI 冒烟测试")
    parser.add_argument("--launch", action="store_true", help="真实启动悬浮浏览器并自动关闭")
    args = parser.parse_args()

    from PySide6.QtWidgets import QApplication
    from ok.ui.qt.common.style_sheet import StyleSheet  # noqa: F401

    app = QApplication(sys.argv)
    results: list[str] = []
    failures = 0

    def report(ok: bool, message: str) -> None:
        nonlocal failures
        results.append(f"{'[OK]  ' if ok else '[FAIL]'} {message}")
        if not ok:
            failures += 1

    # 构建标签页
    try:
        from src.gui.floating_browser.tab import FloatingBrowserTab

        tab = FloatingBrowserTab()
        report(True, "FloatingBrowserTab 实例化成功")
    except Exception as error:
        import traceback

        report(False, f"FloatingBrowserTab 实例化失败: {error}")
        results.append(traceback.format_exc())
        print("\n".join(results))
        return 1

    # 控件齐备性（尺寸/透明度/播放控制已转移到悬浮窗自带工具条，此处不应再有）
    for attribute, label in [
        ("url_edit", "网址输入框"),
        ("start_button", "启动按钮"),
        ("stop_button", "停止按钮"),
        ("hide_button", "显示/隐藏按钮"),
        ("save_button", "保存按钮"),
        ("status_title", "状态标题"),
    ]:
        report(hasattr(tab, attribute), f"控件存在: {label}")

    for removed, label in [
        ("width_spin", "宽度输入框"),
        ("height_spin", "高度输入框"),
        ("opacity_slider", "透明度滑块"),
        ("on_top_switch", "置顶开关"),
    ]:
        report(not hasattr(tab, removed), f"数字/开关控件已移除: {label}")

    report(len(tab.hotkey_edits) == 8, f"热键输入框数量为 8（实际 {len(tab.hotkey_edits)}）")
    report(hasattr(tab, "hover_opacity_spin"), "控件存在: 穿透悬停透明度")
    report(hasattr(tab, "log_output_check"), "控件存在: 输出悬浮浏览器日志（总开关）")
    # 弹幕/字幕开关已经移到悬浮窗工具条上，ok 界面里不应该再有它们
    report(not hasattr(tab, "mirror_check"), "ok 界面已移除旧的「弹幕/字幕映射」勾选框")
    report(not hasattr(tab, "mirror_danmaku_check"), "ok 界面已移除「映射弹幕」勾选框")
    report(not hasattr(tab, "mirror_subtitle_check"), "ok 界面已移除「映射字幕」勾选框")
    # 总开关连通性：勾选框驱动 service -> fb_log
    from src.gui.floating_browser import log as fb_log
    tab.log_output_check.setChecked(True)
    tab._on_log_output_changed()
    report(fb_log.is_verbose() is True, "勾选后日志总开关被打开")
    tab.log_output_check.setChecked(False)
    tab._on_log_output_changed()
    report(fb_log.is_verbose() is False, "取消勾选后日志总开关关闭")

    from src.gui.floating_browser.tab import PRESET_SITES

    names = [name for name, _ in PRESET_SITES]
    report("腾讯视频" not in names, f"预设站点不含腾讯视频（实际 {names}）")

    # 配置读写
    try:
        tab._load_config()
        report(True, "配置加载无异常")
    except Exception as error:
        report(False, f"配置加载异常: {error}")

    # 状态渲染
    try:
        from src.gui.floating_browser.browser import BrowserState

        tab._render_state(BrowserState(found=True, paused=False, current=61, duration=300, rate=1.5))
        title = tab.status_title.text()
        report("01:01" in title and "1.50x" in tab.status_detail.text(),
               f"状态渲染: {title} | {tab.status_detail.text()}")
        tab._render_state(BrowserState(found=False))
        # 文案跟着 i18n 走：有 ok app 时是译文，没有时是英文原文，
        # 所以两种都接受，别把断言写死在某一种语言上
        empty_title = tab.status_title.text()
        report(("no video detected" in empty_title.lower()) or ("未检测到视频" in empty_title),
               f"未找到视频时的提示（{empty_title!r}）")
    except Exception as error:
        report(False, f"状态渲染异常: {error}")

    if args.launch:
        try:
            from src.gui.floating_browser.service import FloatingBrowserService

            service = FloatingBrowserService.instance()
            mode = service.start("about:blank")
            report(service.running, f"service.start() 运行中: {service.running}（内嵌模式={mode}）")
            report(service.set_click_through(True) is None or True, "set_click_through 调用无异常")
            service.set_click_through(False)

            # 验证：穿透通知必须经信号切回主线程，不能在后台线程直接操作 Qt UI
            # （曾导致 "Cannot set parent, new parent is in a different thread"）
            threading.Thread(
                target=lambda: tab._on_ui_event({"click_through": True}),
                daemon=True,
            ).start()
            deadline = time.time() + 2.0
            while time.time() < deadline:
                app.processEvents()
                time.sleep(0.05)
            report(True, "穿透通知经信号切回主线程，无跨线程异常")

            # 窗口句柄解析 + 几何回传是异步的，等它落地再断言
            geometry = service.geometry()
            deadline = time.time() + 8.0
            while time.time() < deadline and geometry[0] < 0:
                time.sleep(0.3)
                geometry = service.geometry()
            report(
                len(geometry) == 4 and geometry[0] >= 0 and geometry[2] > 0,
                f"geometry 读取正常: {geometry}",
            )
            service.apply_opacity(0.75)
            service.refresh_state()
            tab._render_state(service.refresh_state())
            service.stop()
            report(not service.running, "service.stop() 后已停止")
        except Exception as error:
            import traceback

            report(False, f"启动链路异常: {error}")
            results.append(traceback.format_exc())

    print("\n".join(results))
    print(f"\n==== 失败项合计: {failures} ====")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
