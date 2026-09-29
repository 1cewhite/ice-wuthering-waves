"""启动完整性检查。

加载 config.py，验证：
- 全局配置项（含悬浮浏览器）都能被注册；
- 自定义标签（CharacterCodeTab / FloatingBrowserTab）都能被解析；
- 悬浮浏览器热键默认值合法。

用法：
    .\\.venv\\Scripts\\python.exe tests/floating_browser_integration.py
"""

from __future__ import annotations

import os
import sys
import traceback

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

import importlib  # noqa: E402

failures = 0
lines: list[str] = []


def report(ok: bool, message: str) -> None:
    global failures
    lines.append(f"{'[OK]  ' if ok else '[FAIL]'} {message}")
    if not ok:
        failures += 1


def main() -> int:
    # 1) 配置加载
    try:
        import config as app_config

        cfg = app_config.config
        report(True, "config.py 加载成功")
    except Exception as error:
        report(False, f"config.py 加载失败: {error}")
        lines.append(traceback.format_exc())
        print("\n".join(lines))
        return 1

    # 2) 全局配置项
    names = [option.name for option in cfg.get("global_configs", [])]
    report("Floating Browser" in names, f"全局配置含 'Floating Browser'（共 {len(names)} 项）")
    report("Floating Browser Hotkey" in names, "全局配置含 'Floating Browser Hotkey'")

    # 3) 悬浮浏览器配置默认值
    try:
        browser_option = next(o for o in cfg["global_configs"] if o.name == "Floating Browser")
        defaults = browser_option.default_config
        report(isinstance(defaults.get("width"), int), f"width 默认值为整数: {defaults.get('width')}")
        report(0.2 <= float(defaults.get("opacity", 0)) <= 1.0, f"opacity 默认值合法: {defaults.get('opacity')}")
        report(bool(defaults.get("url")), f"url 默认值非空: {defaults.get('url')}")
    except Exception as error:
        report(False, f"读取 Floating Browser 配置失败: {error}")

    # 4) 热键默认值合法
    try:
        from src.gui.floating_browser.hotkeys import HOTKEY_ACTIONS, parse_hotkey

        hotkey_option = next(o for o in cfg["global_configs"] if o.name == "Floating Browser Hotkey")
        bad = []
        for action in HOTKEY_ACTIONS:
            value = hotkey_option.default_config.get(action)
            parsed = parse_hotkey(value)
            if parsed is None:
                bad.append(f"{action}={value}")
            else:
                # 与 HOTKEY_ACTIONS 中的默认值保持一致
                if value != HOTKEY_ACTIONS[action]["default"]:
                    bad.append(f"{action} 配置({value}) 与动作默认({HOTKEY_ACTIONS[action]['default']}) 不一致")
        report(not bad, "热键默认值全部可解析且一致" if not bad else f"热键问题: {bad}")
    except Exception as error:
        report(False, f"热键默认值检查失败: {error}")

    # 5) 自定义标签解析
    # 注意：这里只做「模块导入 + 类解析」，不实例化 —— 实例化需要 Qt 事件循环，
    # 在纯命令行环境下会崩溃。真正的实例化由框架在 MainWindow 中完成。
    for module_path, class_name in cfg.get("custom_tabs", []):
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            report(
                cls.__name__ == class_name and issubclass(cls, object),
                f"自定义标签 {module_path}.{class_name} 解析成功",
            )
        except Exception as error:
            report(False, f"自定义标签 {module_path}.{class_name} 解析失败: {error}")

    # 6) 服务单例
    try:
        from src.gui.floating_browser.service import FloatingBrowserService

        service = FloatingBrowserService.instance()
        report(service is FloatingBrowserService.instance(), "悬浮浏览器服务为单例")
        report(not service.running, "初始状态未运行")
    except Exception as error:
        report(False, f"服务初始化失败: {error}")

    print("\n".join(lines))
    print(f"\n==== 失败项合计: {failures} ====")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
