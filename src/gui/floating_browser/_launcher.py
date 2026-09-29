"""悬浮浏览器子进程的独立入口（由 ``subprocess`` 以脚本方式启动）。

为什么不用 ``multiprocessing``？
--------------------------------
``multiprocessing`` 的 ``spawn`` 会把 ``__main__`` 重新导入一次。宿主是 Qt 应用
（``ok`` 框架）时，这一步会让子进程卡在引导阶段——进程活着，但目标函数
永远不执行，既不发 ``ready`` 也不报错，悬浮窗因此从不出现。实测确认：
同样的代码在不导入 Qt 时 3.7 秒就绪，导入 Qt 后完全静默。

改用 ``subprocess`` 启动一个**全新解释器**执行本文件，就没有 ``__main__``
重导入问题。

IPC 选择
--------
用 ``stdin/stdout`` 的按行 JSON，而不是 TCP socket：
Windows 上 ``multiprocessing.connection`` 的 loopback 连接偶发握手失败
（子进程收到 ``EOFError``）。管道是最稳定的方式，且天然与父进程生命周期绑定。

协议（每行一个 JSON 对象）
--------------------------
父 -> 子 (stdin):  ``{"cmd": "...", "arg": ...}``
子 -> 父 (stdout): ``{"kind": "...", "payload": ...}``
"""

from __future__ import annotations

import json
import os
import sys

# 允许以脚本方式（python this_file.py）运行
_FILE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_FILE_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


class _StdinReceiver:
    """把父进程写在 stdin 上的 JSON 行包装成 ``.get(timeout)`` 接口。"""

    def __init__(self, stream):
        self._stream = stream
        self._buffer = b""

    def get(self, timeout=None):
        # 阻塞式读取一行；timeout 仅用于兼容 queue 的调用签名
        while True:
            if b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                message = json.loads(line.decode("utf-8"))
                return message.get("cmd"), message.get("arg")
            chunk = self._stream.readline()
            if not chunk:
                raise EOFError("父进程已关闭输入流")
            self._buffer += chunk


# 协议行前缀：子进程内可能有其它库往 stdout 打印（如 ok 框架的启动横幅），
# 用前缀把它们和协议消息区分开。
PROTOCOL_PREFIX = "@@OKFB@@"


class _StdoutSender:
    """把消息写成 stdout 上的 JSON 行，并带上协议前缀。"""

    def __init__(self, stream):
        self._stream = stream

    def put(self, item):
        try:
            kind, payload = item
            line = json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False)
            self._stream.write((PROTOCOL_PREFIX + line + "\n").encode("utf-8"))
            self._stream.flush()
        except Exception:
            pass


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    raw = os.environ.get("OK_FLOATING_BROWSER_CONFIG", "{}")
    try:
        config = json.loads(raw)
    except Exception:
        config = {}

    receiver = _StdinReceiver(sys.stdin.buffer)
    sender = _StdoutSender(sys.stdout.buffer)

    # 与父进程共用同一个日志总开关；关闭时不往控制台写任何东西。
    # （启动失败仍会通过 sender 把错误报给父进程，由父进程在界面上提示。）
    verbose = bool(config.get("verbose"))

    def _trace(message: str) -> None:
        if not verbose:
            return
        try:
            sys.stderr.write(f"@@LAUNCHER@@ {message}\n")
            sys.stderr.flush()
        except Exception:
            pass

    _trace("starting import")
    try:
        from src.gui.floating_browser.webview_process import run_browser_process

        _trace("import ok, entering run_browser_process")
        run_browser_process(config, receiver, sender)
        _trace("run_browser_process returned")
    except Exception as error:
        import traceback

        _trace(f"EXCEPTION {error!r}")
        sender.put(("error", f"子进程异常: {error}"))
        if verbose:
            try:
                sys.stderr.write(traceback.format_exc())
                sys.stderr.flush()
            except Exception:
                pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
