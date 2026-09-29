"""悬浮浏览器模块。

提供一个可以悬浮在游戏窗口之上的浏览器窗格，用来播放视频（B站、YouTube 等），
并支持通过全局快捷键控制视频的播放/暂停、快进、后退与倍速。

模块组成：
- ``FloatingBrowser``：核心窗口，负责创建窗口、设置尺寸/透明度、注入 JS 控制视频。
- ``HotkeyManager``：注册 Windows 全局热键并分发到对应动作。
- ``FloatingBrowserTab``：图形化配置界面，供用户调整参数并预览。
"""

from .browser import FloatingBrowser, BrowserState
from .hotkeys import HotkeyManager, HOTKEY_ACTIONS
from .tab import FloatingBrowserTab

__all__ = [
    "FloatingBrowser",
    "BrowserState",
    "HotkeyManager",
    "HOTKEY_ACTIONS",
    "FloatingBrowserTab",
]
