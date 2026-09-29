"""悬浮浏览器的统一日志出口。

整个悬浮浏览器功能（主进程 + 子进程）往**控制台 / 日志文件**写的内容全部经过这里，
由 ``verbose`` **一个开关**统一控制：

- 主进程：``set_verbose(True/False)``，由配置项 ``Floating Browser -> log_output`` 驱动；
  配置页「输出悬浮浏览器日志」勾选框改完即时生效，不用重启。
- 子进程：父进程把 ``verbose`` 放进启动配置（``run_browser_process`` 里再设一次），
  启动器 ``_launcher.py`` 自己也会读同一个字段。

默认**关闭**（静默）：不打开开关时，控制台和日志文件里不会出现悬浮浏览器的任何输出。

注意：开关只影响「日志文本」。界面上给用户看的提示（ok 主窗口的 InfoBar、
状态栏文字）走的是另一条通道，不受影响 —— 所以启动失败、热键被占用这类关键信息
即使日志静默，仍然会以弹窗形式告诉用户。
"""

from __future__ import annotations

import sys

# 故意延迟取 ok 的 Logger：子进程（webview_process）只用到 trace()，
# 没必要为了日志把 ok 框架整个拖进子进程。
_logger = None


def _get_logger():
    global _logger
    if _logger is None:
        try:
            from ok import Logger

            _logger = Logger.get_logger(__name__)
        except Exception:  # pragma: no cover - 极端环境下退回标准库
            import logging

            _logger = logging.getLogger(__name__)
    return _logger


_verbose = False


def set_verbose(enabled) -> None:
    """打开 / 关闭悬浮浏览器的全部日志输出。"""
    global _verbose
    _verbose = bool(enabled)


def is_verbose() -> bool:
    return _verbose


def info(message) -> None:
    if _verbose:
        _get_logger().info(message)


def debug(message) -> None:
    if _verbose:
        _get_logger().debug(message)


def warning(message) -> None:
    if _verbose:
        _get_logger().warning(message)


def error(message) -> None:
    if _verbose:
        _get_logger().error(message)


def trace(message) -> None:
    """子进程诊断信息：直接写 stderr（父进程收集进日志）。"""
    if not _verbose:
        return
    try:
        sys.stderr.write(f"@@TRACE@@ {message}\n")
        sys.stderr.flush()
    except Exception:
        pass
