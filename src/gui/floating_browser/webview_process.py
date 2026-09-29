"""独立进程运行 pywebview 的子进程入口。

为什么需要单独一个进程？
------------------------
``pywebview.start()`` 会阻塞调用线程，并且**必须在主线程**执行。
而主程序使用 Qt 事件循环，两者无法共存于同一线程。因此这里把悬浮浏览器
放进独立子进程：子进程拥有自己的主线程与消息循环，主进程通过队列下发指令、
回收状态，互不干扰。

协议
----
主进程 -> 子进程（队列 ``command_queue``）：
    ("set_size", (width, height))
    ("set_opacity", opacity)
    ("set_position", (x, y))
    ("set_geometry", (x, y, width, height))   # 拖动/缩放后的回写
    ("set_click_through", bool)               # 鼠标穿透
    ("set_game_hwnd", hwnd)                   # 游戏窗口句柄（弹幕覆盖层锚点）
    ("set_mirror", bool)                      # 弹幕/字幕映射到最上层
    ("set_on_top", bool)
    ("set_interactive", bool)                 # 临时抢回交互（穿透时用）
    ("load_url", url)
    ("show" | "hide" | "toggle_visible" | "refresh" | "play_pause")
    ("seek", seconds)
    ("set_rate", rate)
    ("hold_rate", rate)                       # 按住倍速：锁定倍速直到 release_hold
    ("release_hold", None)                    # 松开：恢复按住前的倍速
    ("speed_up" | "slow_down")
    ("set_volume", volume)
    ("toggle_mute", None)
    ("quit", None)

子进程 -> 主进程（队列 ``status_queue``）：
    ("ready", True)               # 窗口与注入脚本就绪
    ("error", message)            # 初始化失败
    ("state", {..})               # 视频状态快照
    ("geometry", (x, y, w, h))    # 用户在悬浮窗上拖动/缩放产生的新几何
    ("ui", {...})                 # 悬浮工具条交互回传（透明度、穿透、隐藏等）
    ("closed", True)              # 用户关闭了窗口
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from typing import Any

from src.gui.floating_browser import log as fb_log
from src.gui.floating_browser import bilibili_danmaku
from src.gui.floating_browser.danmaku_overlay import MIRROR_JS, DanmakuOverlay
from src.gui.floating_browser.subtitle_overlay import SubtitleOverlay

try:  # Windows 专用；其它平台走降级分支
    import ctypes
    from ctypes import wintypes
except Exception:  # pragma: no cover - 非 Windows
    ctypes = None  # type: ignore[assignment]
    wintypes = None  # type: ignore[assignment]

if ctypes is not None:
    # 统一声明 Win32 函数签名，避免 HWND 被默认按 32 位 c_int 截断：
    # 64 位句柄高位被砍掉后，SetWindowPos/SetLayeredWindowAttributes 会
    # 作用到错误的窗口（或直接失败），表现为「窗口不可见 / 消失」。
    _user32 = ctypes.windll.user32
    _user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
    _user32.SetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint,
    ]
    _user32.SetWindowPos.restype = ctypes.c_int
    _user32.SetLayeredWindowAttributes.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_uint,
    ]
    _user32.SetLayeredWindowAttributes.restype = ctypes.c_int
    _user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.GetWindowRect.restype = ctypes.c_int
    _user32.IsIconic.argtypes = [ctypes.c_void_p]
    _user32.IsIconic.restype = ctypes.c_int
    _user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.ShowWindow.restype = ctypes.c_int
    # 定位游戏窗口（弹幕覆盖层要锚到「游戏画面」上，而不是悬浮窗上）
    _user32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.EnumWindows.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.IsWindow.argtypes = [ctypes.c_void_p]
    _user32.IsWindow.restype = ctypes.c_int
    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.IsWindowVisible.restype = ctypes.c_int
    _user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.GetClientRect.restype = ctypes.c_int
    _user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.ClientToScreen.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.GetWindowThreadProcessId.restype = ctypes.c_uint

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_void_p,
    ]
    _kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype = ctypes.c_int

# 与主进程共享的 JS 注入逻辑（避免跨进程 import 业务包）
VIDEO_JS = r"""
(function () {
    if (window.__okFloatingBrowserReady) { return true; }
    window.__okFloatingBrowserReady = true;
    window.__okFindVideos = function () {
        var list = Array.prototype.slice.call(document.querySelectorAll('video'));
        var usable = list.filter(function (v) { return v && (v.duration > 1 || !isFinite(v.duration)); });
        var pool = usable.length ? usable : list;
        if (!pool.length) { return null; }
        var playing = pool.filter(function (v) { return !v.paused && !v.ended; });
        if (playing.length) { return playing[0]; }
        var best = pool[0];
        for (var i = 1; i < pool.length; i++) {
            var d1 = isFinite(best.duration) ? best.duration : 0;
            var d2 = isFinite(pool[i].duration) ? pool[i].duration : 0;
            if (d2 > d1) { best = pool[i]; }
        }
        return best;
    };
    window.__okStatus = function () {
        var v = window.__okFindVideos();
        if (!v) { return {found:false, paused:true, current:0, duration:0, rate:1, volume:1, muted:false, title:document.title||''}; }
        return {found:true, paused:v.paused, current:(isFinite(v.currentTime)?v.currentTime:0),
                duration:(isFinite(v.duration)?v.duration:0), rate:v.playbackRate, volume:v.volume,
                muted:v.muted, title:document.title||''};
    };
    window.__okPlayPause = function () {
        var v = window.__okFindVideos(); if (!v) { return null; }
        if (v.paused || v.ended) { var p = v.play(); if (p && p.catch) { p.catch(function(){}); } } else { v.pause(); }
        return window.__okStatus();
    };
    window.__okSeek = function (delta) {
        var v = window.__okFindVideos(); if (!v) { return null; }
        var t = v.currentTime + delta; if (t < 0) { t = 0; }
        if (isFinite(v.duration) && t > v.duration) { t = v.duration; }
        try { v.currentTime = t; } catch (e) {}
        return window.__okStatus();
    };
    window.__okSetRate = function (rate) {
        var v = window.__okFindVideos(); if (!v) { return null; }
        if (rate < 0.25) { rate = 0.25; } if (rate > 16) { rate = 16; }
        v.playbackRate = rate; return window.__okStatus();
    };
    // ---- 「按住倍速」--------------------------------------------------------
    // bilibili 这类自定义播放器会在重新初始化 / 切集 / 跳转时把 playbackRate
    // 重置回 1.0，一次性赋值会被吃掉。所以按住期间用一个定时器持续锁定目标倍速，
    // 松开时恢复「按住之前」的倍速（原倍速记在 JS 侧，不依赖主进程的缓存状态）。
    window.__okHoldTarget = null;
    window.__okHoldPrev = null;
    window.__okHoldTimer = null;
    window.__okHoldSafety = null;
    window.__okHoldEnforce = function () {
        if (window.__okHoldTarget == null) { return; }
        var v = window.__okFindVideos(); if (!v) { return; }
        if (Math.abs(v.playbackRate - window.__okHoldTarget) > 0.001) {
            try { v.playbackRate = window.__okHoldTarget; } catch (e) {}
        }
    };
    window.__okReleaseHold = function () {
        var prev = window.__okHoldPrev;
        window.__okHoldTarget = null;
        window.__okHoldPrev = null;
        if (window.__okHoldTimer != null) { clearInterval(window.__okHoldTimer); window.__okHoldTimer = null; }
        if (window.__okHoldSafety != null) { clearTimeout(window.__okHoldSafety); window.__okHoldSafety = null; }
        var v = window.__okFindVideos();
        if (v) { try { v.playbackRate = (prev == null ? 1.0 : prev); } catch (e) {} }
        return window.__okStatus();
    };
    window.__okHoldRate = function (rate) {
        if (rate < 0.25) { rate = 0.25; } if (rate > 16) { rate = 16; }
        var v = window.__okFindVideos();
        window.__okHoldPrev = v ? v.playbackRate : null;
        window.__okHoldTarget = rate;
        window.__okHoldEnforce();
        if (window.__okHoldTimer == null) {
            window.__okHoldTimer = setInterval(window.__okHoldEnforce, 400);
        }
        // 安全网：万一「松开」事件丢失（钩子被系统卸载等），最多锁定 2 分钟
        if (window.__okHoldSafety != null) { clearTimeout(window.__okHoldSafety); }
        window.__okHoldSafety = setTimeout(window.__okReleaseHold, 120000);
        return window.__okStatus();
    };
    window.__okSetVolume = function (vol) {
        var v = window.__okFindVideos(); if (!v) { return null; }
        v.volume = Math.max(0, Math.min(1, vol)); return window.__okStatus();
    };
    window.__okToggleMute = function () {
        var v = window.__okFindVideos(); if (!v) { return null; }
        v.muted = !v.muted; return window.__okStatus();
    };
    window.addEventListener('beforeunload', function (e) { e.stopImmediatePropagation(); }, true);
    return true;
})();
"""

# ---------------------------------------------------------------------------
# 悬浮工具条：把「拖动 / 缩放 / 穿透 / 透明度 / 隐藏」放到悬浮窗自己身上
# ---------------------------------------------------------------------------
# 工具条是浮在网页顶部的一条（不预留空间，网页尺寸不因此变化），
# 高度由 TOOLBAR_HEIGHT 统一控制（Python 侧注入到 JS 里的 BAR_HEIGHT）。
TOOLBAR_HEIGHT = 28
_BAR_HEIGHT_TOKEN = "__OK_BAR_HEIGHT__"

# 关键点：页面本身会吃掉鼠标事件，因此工具条上需要自己监听 mousemove，
# 一旦指针越过工具条下沿就立刻通过 pywebview 的 JS 桥通知 Python 放宽裁剪区，
# 否则会出现「鼠标在窗口下半部分点不动」的问题。
CONTROL_BAR_JS = r"""
(function () {
    if (window.__okBarInstalled) { return true; }
    window.__okBarInstalled = true;

    var BAR_HEIGHT = __OK_BAR_HEIGHT__;
    var state = { top: 0, left: 0, width: 0, height: 0, w: 0, h: 0 };
    // 工具条图标（内联 SVG，用 currentColor 跟随主题）
    var ICON_PIN = '<svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M4.146.146A.5.5 0 0 1 4.5 0h7a.5.5 0 0 1 .5.5c0 .68-.342 1.174-.646 1.479-.126.125-.25.224-.354.298v4.431l.078.048c.203.127.476.314.751.555C12.36 7.775 13 8.527 13 9.5a.5.5 0 0 1-.5.5h-4v4.5a.5.5 0 0 1-1 0V10h-4a.5.5 0 0 1-.5-.5c0-.973.64-1.725 1.17-2.189A5.9 5.9 0 0 1 5 6.708V2.277a3 3 0 0 1-.354-.298C4.342 1.674 4 1.179 4 .5a.5.5 0 0 1 .146-.354z"/></svg>';
    var ICON_MIN = '<svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><rect x="3" y="7.25" width="10" height="1.5" rx="0.75"/></svg>';
    var ICON_DM = '<svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor"><path d="M3 2.5h10A1.5 1.5 0 0 1 14.5 4v5.5A1.5 1.5 0 0 1 13 11H8.6L5.2 14v-3H3a1.5 1.5 0 0 1-1.5-1.5V4A1.5 1.5 0 0 1 3 2.5z"/><path d="M4.6 5.6h6.8v1.2H4.6z" fill="#101216"/><path d="M4.6 8h4.4v1.2H4.6z" fill="#101216"/></svg>';
    var ICON_CC = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><rect x="1.7" y="3.2" width="12.6" height="9.6" rx="1.8"/><path d="M4.6 8.3h2.3M9.1 8.3h2.3M6.4 11h3.2"/></svg>';
    var ICON_CLOSE = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M4.2 4.2 L11.8 11.8 M11.8 4.2 L4.2 11.8"/></svg>';
    var css = document.createElement('style');
    css.textContent = [
        '#__ok_xbar{position:fixed;left:0;top:0;width:100%;height:' + BAR_HEIGHT + 'px;',
        'background:linear-gradient(to bottom,rgba(18,20,26,.96),rgba(18,20,26,.82));',
        'font:12px/1 "Microsoft YaHei",system-ui,sans-serif;color:#e8eaf0;',
        'display:flex;align-items:center;gap:6px;padding:0 8px;box-sizing:border-box;',
        'user-select:none;z-index:2147483647;border-bottom:1px solid rgba(255,255,255,.10);}',
        '#__ok_xbar .ok-drag{flex:1;height:100%;cursor:move;display:flex;align-items:center;',
        'overflow:hidden;white-space:nowrap;text-overflow:ellipsis;opacity:.75;padding:0 6px;}',
        '#__ok_xbar button{all:unset;cursor:pointer;padding:4px 7px;border-radius:5px;',
        'background:rgba(255,255,255,.10);color:#e8eaf0;font-size:12px;line-height:1;',
        'transition:background .15s;display:inline-flex;align-items:center;justify-content:center;}',
        '#__ok_xbar button:hover{background:rgba(255,255,255,.22);}',
        '#__ok_xbar button.on{background:#2f6feb;color:#fff;}',
        '#__ok_xbar button.ok-icon{width:22px;height:22px;padding:0;flex:0 0 auto;}',
        '#__ok_xbar button.ok-close:hover{background:rgba(220,60,60,.92);color:#fff;}',
        '#__ok_xbar svg{display:block;pointer-events:none;}',
        '#__ok_xbar input[type=range]{width:74px;accent-color:#2f6feb;cursor:pointer;margin:0;}',
        '#__ok_xbar .ok-sep{width:1px;height:16px;background:rgba(255,255,255,.18);}',
        '#__ok_panel{position:fixed;width:250px;max-height:calc(100vh - 52px);overflow-y:auto;',
        'background:rgba(24,25,28,.97);border:1px solid rgba(255,255,255,.12);border-radius:8px;',
        'box-shadow:0 10px 30px rgba(0,0,0,.5);z-index:2147483647;padding:0;',
        'font:12px/1.5 "Microsoft YaHei",system-ui,sans-serif;color:#e8eaf0;}',
        '#__ok_panel .ok-ph{display:flex;align-items:center;justify-content:space-between;',
        'padding:9px 12px;border-bottom:1px solid rgba(255,255,255,.08);font-size:13px;}',
        '#__ok_panel .ok-pr{all:unset;cursor:pointer;color:#8b8f9a;font-size:11px;}',
        '#__ok_panel .ok-pr:hover{color:#00a1d6;}',
        '#__ok_panel .ok-pb{padding:11px 12px 5px;}',
        '#__ok_panel .ok-row{margin-bottom:14px;}',
        '#__ok_panel .ok-label{display:flex;justify-content:space-between;align-items:center;',
        'color:#c9ccd6;margin-bottom:6px;}',
        '#__ok_panel .ok-val{color:#00a1d6;font-variant-numeric:tabular-nums;}',
        '#__ok_panel input[type=range]{width:100%;height:16px;accent-color:#00a1d6;cursor:pointer;margin:0;}',
        '#__ok_panel .ok-chips{display:flex;gap:8px;}',
        '#__ok_panel .ok-chip{flex:1;text-align:center;padding:7px 0;border-radius:6px;cursor:pointer;',
        'background:rgba(255,255,255,.08);color:#c9ccd6;user-select:none;transition:background .15s;}',
        '#__ok_panel .ok-chip:hover{background:rgba(255,255,255,.16);}',
        '#__ok_panel .ok-chip.on{background:#00a1d6;color:#fff;}',
        '#__ok_panel .ok-seg{display:flex;gap:4px;}',
        '#__ok_panel .ok-seg span{flex:1;text-align:center;padding:5px 0;border-radius:5px;cursor:pointer;',
        'background:rgba(255,255,255,.08);color:#c9ccd6;font-size:11px;user-select:none;}',
        '#__ok_panel .ok-seg span.on{background:#00a1d6;color:#fff;}',
        '#__ok_panel .ok-hint{color:#7b7f8a;font-size:11px;margin:-6px 0 12px;}',
        '#__ok_panel .ok-note{color:#7b7f8a;font-size:11px;margin-bottom:10px;line-height:1.5;}',
        // 保留页面原生滚动条但收窄到 8px；右侧把手加宽到 18~22px，
        // 这样滚动条只盖住把手最外侧 8px，把手内侧仍可抓取（滚动条是原生层，无法被盖住）。
        '::-webkit-scrollbar{width:8px;height:8px;background:transparent;}',
        '::-webkit-scrollbar-thumb{background:rgba(160,170,185,.5);border-radius:4px;}',
        '::-webkit-scrollbar-thumb:hover{background:rgba(180,190,205,.75);}',
        // 缩放把手贴窗口最外侧边缘（顶部那一条是工具条，故 w/e/nw/ne 从工具条下沿开始）
        '.ok-resize{position:fixed;z-index:2147483647;box-sizing:border-box;}',
        '.ok-resize.w{top:' + BAR_HEIGHT + 'px;left:0;width:8px;height:calc(100% - ' + (BAR_HEIGHT + 8) + 'px);cursor:w-resize;}',
        '.ok-resize.e{top:' + BAR_HEIGHT + 'px;right:0;width:18px;height:calc(100% - ' + (BAR_HEIGHT + 16) + 'px);cursor:e-resize;}',
        '.ok-resize.s{left:0;bottom:0;width:100%;height:14px;cursor:s-resize;}',
        '.ok-resize.nw{top:' + BAR_HEIGHT + 'px;left:0;width:18px;height:18px;cursor:nw-resize;}',
        '.ok-resize.ne{top:' + BAR_HEIGHT + 'px;right:0;width:22px;height:22px;cursor:ne-resize;}',
        '.ok-resize.sw{left:0;bottom:0;width:22px;height:22px;cursor:sw-resize;}',
        '.ok-resize.se{right:0;bottom:0;width:22px;height:22px;cursor:se-resize;}',
    ].join('');
    (document.head || document.documentElement).appendChild(css);

    function push(payload) {
        if (window.pywebview && window.pywebview.api && window.pywebview.api.ui) {
            try { window.pywebview.api.ui(payload); } catch (e) {}
        }
    }

    function clampInt(value, low, high, fallback) {
        var number = parseInt(value, 10);
        if (isNaN(number)) { return fallback; }
        return Math.max(low, Math.min(high, number));
    }

    // 拖动时用 requestAnimationFrame 节流，避免 mousemove 洪水压垮 JS 桥。
    // 重要：drag_start / drag_end 必须立即送达，不能被合并掉——否则
    // Python 侧拿不到起始矩形，后续 drag_move 全部变成空操作。
    var sendPending = false;
    var lastPayload = null;
    function sendDrag(payload) {
        if (payload.action !== 'drag_move') {
            push(payload);
            return;
        }
        lastPayload = payload;
        if (sendPending) { return; }
        sendPending = true;
        var flush = function () {
            sendPending = false;
            if (lastPayload) { push(lastPayload); lastPayload = null; }
        };
        if (window.requestAnimationFrame) {
            window.requestAnimationFrame(flush);
        } else {
            setTimeout(flush, 16);
        }
    }

    function build() {
        if (!document.body) { return; }
        if (document.getElementById('__ok_xbar')) { return; }
        var bar = document.createElement('div');
        bar.id = '__ok_xbar';
        bar.innerHTML = [
            '<div class="ok-drag" id="__ok_drag">拖动此处移动窗口</div>',
            '<span class="ok-sep"></span>',
            '<input type="range" id="__ok_op" min="20" max="100" value="90" title="窗口透明度">',
            '<button id="__ok_ct" title="鼠标穿透：开启后点击会直接落到下面的窗口">穿透</button>',
            '<span class="ok-sep"></span>',
            '<button id="__ok_dm" class="ok-icon" title="映射弹幕到游戏画面最上层（点一下开/关）">' + ICON_DM + '</button>',
            '<button id="__ok_cc" class="ok-icon" title="映射字幕到游戏画面最上层（点一下开/关；需先在播放器里打开 CC）">' + ICON_CC + '</button>',
            '<button id="__ok_top" class="ok-icon" title="切换置顶">' + ICON_PIN + '</button>',
            '<button id="__ok_hide" class="ok-icon" title="隐藏悬浮窗（可在ok界面再显示）">' + ICON_MIN + '</button>',
            '<button id="__ok_close" class="ok-icon ok-close" title="关闭悬浮浏览器">' + ICON_CLOSE + '</button>',
        ].join('');
        document.body.appendChild(bar);

        var panel = document.createElement('div');
        panel.id = '__ok_panel';
        panel.style.display = 'none';
        panel.innerHTML = '<div class="ok-ph"><span id="__ok_pt">弹幕设置</span>' +
                          '<button class="ok-pr" id="__ok_pr">重置</button></div>' +
                          '<div class="ok-pb" id="__ok_pb"></div>';
        document.body.appendChild(panel);

        ['w', 'e', 's', 'nw', 'ne', 'sw', 'se'].forEach(function (edge) {
            var grip = document.createElement('div');
            grip.className = 'ok-resize ' + edge;
            document.body.appendChild(grip);
        });

        var opacity = document.getElementById('__ok_op');
        var clickThrough = document.getElementById('__ok_ct');
        var onTop = document.getElementById('__ok_top');
        var hide = document.getElementById('__ok_hide');
        var closeBtn = document.getElementById('__ok_close');
        var mirrorBtn = document.getElementById('__ok_dm');
        var subtitleBtn = document.getElementById('__ok_cc');
        bindPanelOpeners();

        opacity.addEventListener('input', function () {
            push({ action: 'opacity', value: clampInt(opacity.value, 20, 100, 90) });
        });
        clickThrough.addEventListener('click', function () {
            // 先本地切换外观，再通知 Python，避免依赖一次往返
            var next = !clickThrough.classList.contains('on');
            window.__okSyncButtons(next, onTop.classList.contains('on'), dmOn(), ccOn());
            push({ action: 'toggle_click_through' });
        });
        onTop.addEventListener('click', function () {
            var next = !onTop.classList.contains('on');
            window.__okSyncButtons(clickThrough.classList.contains('on'), next, dmOn(), ccOn());
            push({ action: 'toggle_on_top' });
        });
        mirrorBtn.addEventListener('click', function () {
            // 弹幕映射开关（与字幕开关互不影响）
            mirrorBtn.classList.toggle('on');
            push({ action: 'toggle_mirror_danmaku' });
        });
        subtitleBtn.addEventListener('click', function () {
            subtitleBtn.classList.toggle('on');
            push({ action: 'toggle_mirror_subtitle' });
        });
        hide.addEventListener('click', function () {
            push({ action: 'hide' });
        });
        closeBtn.addEventListener('click', function () {
            push({ action: 'close' });
        });

        bindDrag(document.getElementById('__ok_drag'), 'move');
        ['w', 'e', 's', 'nw', 'ne', 'sw', 'se'].forEach(function (edge) {
            bindDrag(document.querySelector('.ok-resize.' + edge), edge);
        });
        syncButtons();
    }

    // 让按钮外观反映真实状态
    function syncButtons() {
        push({ action: 'query_state' });
    }

    function dmOn() {
        var el = document.getElementById('__ok_dm');
        return !!(el && el.classList.contains('on'));
    }

    function ccOn() {
        var el = document.getElementById('__ok_cc');
        return !!(el && el.classList.contains('on'));
    }

    window.__okSyncButtons = function (clickThrough, onTop, mirrorDanmaku, mirrorSubtitle) {
        var ct = document.getElementById('__ok_ct');
        var top = document.getElementById('__ok_top');
        var dm = document.getElementById('__ok_dm');
        var cc = document.getElementById('__ok_cc');
        if (ct) { ct.className = clickThrough ? 'on' : ''; }
        if (top) { top.className = onTop ? 'on' : ''; }
        if (dm) { dm.className = 'ok-icon' + (mirrorDanmaku ? ' on' : ''); }
        if (cc) { cc.className = 'ok-icon' + (mirrorSubtitle ? ' on' : ''); }
        var bar = document.getElementById('__ok_xbar');
        if (bar) { bar.style.display = clickThrough ? 'none' : ''; }
        var grips = document.querySelectorAll('.ok-resize');
        for (var i = 0; i < grips.length; i++) {
            grips[i].style.display = clickThrough ? 'none' : '';
        }
    };

    // ------------------------------------------------------------------
    // 设置面板：右键工具条上的「弹幕」「字幕」按钮打开
    //
    // 真相源在子进程（它要拿去渲染、还要存配置）。页面这边只负责显示与修改：
    // 打开面板时用最近一次从子进程收到的值，「重置」也只是把值改回默认再上报。
    // ------------------------------------------------------------------
    var SETTINGS_DEFAULTS = {
        danmaku: {filter_scroll: false, filter_fixed: false, area: 100,
                  opacity: 100, font_scale: 0.8, speed_plus: 1.0},
        subtitle: {font_scale: 1.0, position: 88, bg_opacity: 0}
    };
    var SPEED_STEPS = [[0.5, '很慢'], [0.75, '慢'], [1.0, '适中'], [1.5, '快'], [2.0, '很快']];
    var panelKind = null;

    function cloneSettings(kind) {
        var out = {}, src = SETTINGS_DEFAULTS[kind];
        for (var key in src) {
            if (Object.prototype.hasOwnProperty.call(src, key)) { out[key] = src[key]; }
        }
        return out;
    }

    var panelSettings = {danmaku: cloneSettings('danmaku'), subtitle: cloneSettings('subtitle')};
    var SETTINGS_STORE_KEY = 'ok_floating_browser_overlay_settings';

    function loadStoredSettings() {
        try {
            var raw = localStorage.getItem(SETTINGS_STORE_KEY);
            if (!raw) { return; }
            var saved = JSON.parse(raw) || {};
            for (var kind in panelSettings) {
                if (!Object.prototype.hasOwnProperty.call(panelSettings, kind)) { continue; }
                var bucket = saved[kind] || {};
                for (var key in panelSettings[kind]) {
                    if (Object.prototype.hasOwnProperty.call(panelSettings[kind], key)
                        && bucket[key] !== undefined) {
                        panelSettings[kind][key] = bucket[key];
                    }
                }
            }
        } catch (e) {}
    }

    function saveStoredSettings() {
        try { localStorage.setItem(SETTINGS_STORE_KEY, JSON.stringify(panelSettings)); } catch (e) {}
    }

    // 镜像开启时把设置推给子进程（它负责真正渲染）
    window.__okPushSettings = function () {
        push({action: 'overlay_settings',
              data: {kind: 'danmaku', settings: panelSettings.danmaku}});
        push({action: 'overlay_settings',
              data: {kind: 'subtitle', settings: panelSettings.subtitle}});
        return true;
    };

    loadStoredSettings();

    // 子进程推「当前生效的设置」过来
    window.__okApplySettings = function (kind, settings) {
        if (!settings || !panelSettings[kind]) { return false; }
        for (var key in settings) {
            if (Object.prototype.hasOwnProperty.call(settings, key)) {
                panelSettings[kind][key] = settings[key];
            }
        }
        if (panelKind === kind) { renderPanel(kind); }
        saveStoredSettings();
        return true;
    };

    function rowRange(label, key, value, min, max, suffix) {
        return '<div class="ok-row"><div class="ok-label"><span>' + label +
               '</span><span class="ok-val" data-val="' + key + '">' + Math.round(value) + suffix +
               '</span></div><input type="range" data-key="' + key + '" min="' + min +
               '" max="' + max + '" value="' + Math.round(value) + '"></div>';
    }

    function rowChips(label, items) {
        var html = '<div class="ok-row"><div class="ok-label"><span>' + label +
                   '</span></div><div class="ok-chips">';
        for (var i = 0; i < items.length; i++) {
            html += '<div class="ok-chip' + (items[i].on ? ' on' : '') +
                    '" data-toggle="' + items[i].key + '">' + items[i].text + '</div>';
        }
        return html + '</div></div>';
    }

    function rowSeg(label, key, value, steps) {
        var current = '';
        var html = '<div class="ok-row"><div class="ok-label"><span>' + label +
                   '</span><span class="ok-val" data-val="' + key + '"></span></div><div class="ok-seg">';
        for (var i = 0; i < steps.length; i++) {
            if (Math.abs(steps[i][0] - value) < 0.01) { current = steps[i][1]; }
            html += '<span data-set="' + key + '" data-value="' + steps[i][0] + '">' +
                    steps[i][1] + '</span>';
        }
        return html + '</div></div>';
    }

    function renderPanel(kind) {
        var body = document.getElementById('__ok_pb');
        var title = document.getElementById('__ok_pt');
        if (!body || !title) { return; }
        panelKind = kind;
        var s = panelSettings[kind];
        var html = '';
        if (kind === 'danmaku') {
            title.textContent = '弹幕设置';
            html += rowChips('按类型过滤（选中 = 屏蔽）', [
                {key: 'filter_scroll', text: '滚动', on: !!s.filter_scroll},
                {key: 'filter_fixed', text: '固定', on: !!s.filter_fixed}
            ]);
            html += rowRange('显示区域', 'area', s.area, 10, 100, '%');
            html += rowRange('不透明度', 'opacity', s.opacity, 5, 100, '%');
            html += rowRange('弹幕字号', 'font_scale', s.font_scale * 100, 50, 150, '%');
            html += rowSeg('弹幕速度', 'speed_plus', s.speed_plus, SPEED_STEPS);
            html += '<div class="ok-hint">选项与档位对齐 B 站播放器的弹幕设置。</div>';
        } else {
            title.textContent = '字幕设置';
            html += rowRange('字幕大小', 'font_scale', s.font_scale * 100, 50, 200, '%');
            html += rowRange('字幕位置', 'position', s.position, 0, 100, '%');
            html += rowRange('字幕背景不透明度', 'bg_opacity', s.bg_opacity, 0, 100, '%');
            html += '<div class="ok-hint">位置越大越靠下（0 = 贴顶，100 = 贴底）。</div>';
            html += '<div class="ok-note">字幕只能从页面采集（接口未登录拿不到），' +
                    '所以需要在播放器里打开 CC 才会有字幕。</div>';
        }
        body.innerHTML = html;
        bindPanelControls(kind);
    }

    function pushPanelSettings(kind) {
        saveStoredSettings();
        push({action: 'overlay_settings',
              data: {kind: kind, settings: panelSettings[kind]}});
    }

    function setPanelValue(kind, key, value) {
        var s = panelSettings[kind];
        // 字号在面板里用百分比显示，存储用倍数
        s[key] = (key === 'font_scale') ? (value / 100) : value;
        pushPanelSettings(kind);
    }

    function bindPanelControls(kind) {
        var body = document.getElementById('__ok_pb');
        if (!body) { return; }
        var ranges = body.querySelectorAll('input[type=range]');
        for (var i = 0; i < ranges.length; i++) {
            (function (input) {
                input.addEventListener('input', function () {
                    var key = input.getAttribute('data-key');
                    var label = body.querySelector('.ok-val[data-val="' + key + '"]');
                    if (label) { label.textContent = Math.round(input.value) + '%'; }
                    setPanelValue(kind, key, Number(input.value));
                });
            })(ranges[i]);
        }
        var chips = body.querySelectorAll('.ok-chip');
        for (var j = 0; j < chips.length; j++) {
            (function (chip) {
                chip.addEventListener('click', function () {
                    var key = chip.getAttribute('data-toggle');
                    var next = !chip.classList.contains('on');
                    chip.classList.toggle('on', next);
                    var s = panelSettings[kind];
                    s[key] = next;
                    pushPanelSettings(kind);
                });
            })(chips[j]);
        }
        var segs = body.querySelectorAll('.ok-seg span');
        for (var k = 0; k < segs.length; k++) {
            (function (seg) {
                seg.addEventListener('click', function () {
                    var key = seg.getAttribute('data-set');
                    var parent = seg.parentElement;
                    var siblings = parent.querySelectorAll('span');
                    for (var m = 0; m < siblings.length; m++) {
                        siblings[m].classList.toggle('on', siblings[m] === seg);
                    }
                    var label = body.querySelector('.ok-val[data-val="' + key + '"]');
                    if (label) { label.textContent = seg.textContent; }
                    setPanelValue(kind, key, Number(seg.getAttribute('data-value')));
                });
            })(segs[k]);
        }
        // 分段的当前值要补上（renderPanel 里只放了空占位）
        var segRows = body.querySelectorAll('.ok-seg');
        var keys = ['speed_plus'];
        for (var n = 0; n < segRows.length && n < keys.length; n++) {
            var label2 = body.querySelector('.ok-val[data-val="' + keys[n] + '"]');
            var active = segRows[n].querySelector('span.on');
            if (label2 && active) { label2.textContent = active.textContent; }
        }
    }

    function openPanel(kind, anchor) {
        var panel = document.getElementById('__ok_panel');
        if (!panel) { return; }
        if (panelKind === kind && panel.style.display !== 'none') { closePanel(); return; }
        renderPanel(kind);
        panel.style.display = 'block';
        // 贴着工具条下沿弹出，尽量右对齐触发按钮；超出边界就往回收
        var rect = anchor ? anchor.getBoundingClientRect() : null;
        var width = panel.offsetWidth || 250;
        var left = rect ? (rect.right - width) : 8;
        left = Math.max(6, Math.min(Math.max(6, window.innerWidth - width - 6), left));
        panel.style.left = left + 'px';
        panel.style.top = (BAR_HEIGHT + 4) + 'px';
    }

    function closePanel() {
        var panel = document.getElementById('__ok_panel');
        if (panel) { panel.style.display = 'none'; }
        panelKind = null;
    }

    function bindPanelOpeners() {
        var pairs = [[document.getElementById('__ok_dm'), 'danmaku'],
                     [document.getElementById('__ok_cc'), 'subtitle']];
        for (var i = 0; i < pairs.length; i++) {
            (function (button, kind) {
                if (!button) { return; }
                button.addEventListener('contextmenu', function (event) {
                    // 右键打开设置面板（顺便压掉浏览器自己的右键菜单）
                    event.preventDefault();
                    event.stopPropagation();
                    openPanel(kind, button);
                });
            })(pairs[i][0], pairs[i][1]);
        }
        var reset = document.getElementById('__ok_pr');
        if (reset) {
            reset.addEventListener('click', function () {
                if (!panelKind) { return; }
                panelSettings[panelKind] = cloneSettings(panelKind);
                renderPanel(panelKind);
                pushPanelSettings(panelKind);
            });
        }
        document.addEventListener('mousedown', function (event) {
            var panel = document.getElementById('__ok_panel');
            if (!panel || panel.style.display === 'none') { return; }
            if (panel.contains(event.target)) { return; }
            var id = event.target && event.target.id;
            if (id === '__ok_dm' || id === '__ok_cc') { return; }
            closePanel();
        }, true);
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') { closePanel(); }
        });
    }

    function bindDrag(handle, mode) {
        if (!handle) { return; }
        var dragging = false;
        handle.addEventListener('mousedown', function (event) {
            if (event.button !== 0) { return; }
            if (mode === 'move' && event.target.closest && event.target.closest('button,input')) { return; }
            event.preventDefault();
            event.stopPropagation();
            dragging = true;
            sendDrag({ action: 'drag_start', mode: mode, sx: event.screenX, sy: event.screenY });
        });
        handle.addEventListener('mousemove', function (event) {
            if (dragging) {
                sendDrag({ action: 'drag_move', mode: mode, sx: event.screenX, sy: event.screenY });
            } else {
                releaseIfOutside(event);
            }
        });
        document.addEventListener('mousemove', function (event) {
            if (dragging) {
                sendDrag({ action: 'drag_move', mode: mode, sx: event.screenX, sy: event.screenY });
            } else {
                releaseIfOutside(event);
            }
        });
        window.addEventListener('mouseup', function () {
            if (dragging) {
                dragging = false;
                push({ action: 'drag_end', mode: mode });
            }
        });
    }

    // 指针离开工具条区域 -> 让 Python 把点击区域放宽到整窗
    function releaseIfOutside(event) {
        var inside = event.clientY <= BAR_HEIGHT;
        if (inside !== state.top) {
            state.top = inside;
            push({ action: 'hover', value: inside ? 1 : 0 });
        }
    }

    document.addEventListener('mousemove', function (event) {
        releaseIfOutside(event);
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', build);
    } else {
        build();
    }
    window.__okBarRefresh = function () {
        if (!document.getElementById('__ok_xbar')) { build(); }
    };
    return true;
})();
"""
# 把 Python 侧的控制栏高度注入注入脚本，避免两处硬编码 40 不一致
CONTROL_BAR_JS = CONTROL_BAR_JS.replace(_BAR_HEIGHT_TOKEN, str(TOOLBAR_HEIGHT))

# Win32 常量
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0001
SWP_NOSIZE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
LWA_ALPHA = 0x00000002

# 子进程内的全局状态
# 弹幕 / 字幕设置的默认值。选项与档位对齐 B 站播放器的「弹幕设置」面板
# （实测 B 站 localStorage 的 dmSetting：area=50 / opacity=0.7 / fontsize=0.8 /
#  speedplus=1；我们把 area 与 opacity 的默认放宽到 100，保持原本「铺满且不透明」
#  的观感，用户可在面板里调回去）。
DEFAULT_DANMAKU_SETTINGS: dict[str, Any] = {
    "filter_scroll": False,   # 屏蔽滚动弹幕
    "filter_fixed": False,    # 屏蔽固定（顶部/底部）弹幕
    "area": 100,              # 显示区域（占画面高度 %）
    "opacity": 100,           # 不透明度 %
    "font_scale": 0.8,        # 字号倍率（B 站默认 80%）
    "speed_plus": 1.0,        # 速度倍率
}
DEFAULT_SUBTITLE_SETTINGS: dict[str, Any] = {
    "font_scale": 1.0,        # 字幕大小倍率
    "position": 88,           # 垂直位置 %（0=贴顶，100=贴底）
    "bg_opacity": 0,          # 字幕背景不透明度 %
}

_state: dict[str, Any] = {
    "window": None,
    "hwnd": 0,
    "opacity": 0.9,
    "on_top": True,
    "click_through": False,
    "hover_opacity": 0.3,
    "hovering": False,
    "visible": True,
    "pending_geometry": None,
    "mirror_on": False,
    "mirror_danmaku": False,
    "mirror_subtitle": False,
    "overlay": None,
    # 弹幕数据来源：'engine' = 自己拉数据自己算位置（默认目标）；
    # 'dom' = 抄页面元素坐标（拿不到数据时的回退路径）。
    "danmaku_source": "dom",
    "danmaku_cid": 0,            # 当前已经载入引擎的 cid
    "danmaku_loading": False,    # 是否正在后台拉取
    "danmaku_failed": set(),     # 拉取失败的 cid（不反复重试）
    "danmaku_items": 0,          # 载入引擎的弹幕条数
    "danmaku_debug": False,      # 离线调试模式：忽略页面上报的数据源与播放时钟
    # 设置面板里的弹幕/字幕设置（真相源在这里，页面只负责显示与修改）
    "danmaku_settings": dict(DEFAULT_DANMAKU_SETTINGS),
    "subtitle_settings": dict(DEFAULT_SUBTITLE_SETTINGS),
    # 字幕单独一层：弹幕层用颜色键抠图（做不了半透明），字幕要半透明背景，
    # 所以走 UpdateLayeredWindow 的逐像素 alpha。见 subtitle_overlay.py。
    "subtitle_overlay": None,
    "media_t": 0.0,              # 页面上报的播放时刻（诊断用）
    "media_paused": True,
    "media_rate": 1.0,
    "media_seeks": 0,
    # 弹幕覆盖层的锚点：游戏窗口。``game_hwnd_hint`` 是主进程推来的，
    # ``game_hwnd`` 是本进程解析（含超时缓存）后的结果。
    "game_hwnd": 0,
    "game_hwnd_hint": 0,
    "game_hwnd_ts": 0.0,
    "closing": False,
}
# 拉取弹幕的互斥量（避免同一个 cid 被并发拉两次）
_danmaku_lock = threading.Lock()

# 悬浮在窗口顶部的工具条高度（与注入的 HTML 保持一致）
CONTROL_BAR_HEIGHT = 40


# 弹幕横向速度跟踪：给覆盖层「本地补帧」用。
#
# 页面每 60ms 才推一次坐标，若直接照搬，滚动弹幕就是 16.7fps 一段一段地跳
# （每段约 10px），看起来明显卡顿。这里用两次采样估算每条弹幕的横向速度
# （px/s，页面坐标），覆盖层再按 60fps 在本地外推位置 —— 不用增加 IPC 频率。
MOTION_MAX_AGE = 0.5          # 样本超过这么久没更新就丢弃（弹幕已滚出去）
MOTION_MIN_DT = 0.02          # 间隔太短的差分噪声太大
MOTION_MAX_VX = 4000.0        # px/s，超过就当作节点被复用/跳变，不采信
MOTION_SMOOTH = 0.4           # 一阶低通系数（压掉取整抖动）
MOTION_MIN_VX = 20.0          # 小于这个速度视为静止（固定弹幕）
MOTION_EDGE_SLACK = 24        # 贴右边缘这么多像素内 = 正在进场，可直接用整体速度
_motion: dict[str, tuple[float, float, float, str]] = {}
_motion_lock = threading.Lock()

def _attach_velocity(items: list, now: float) -> None:
    """就地给每条弹幕补上横向速度 ``vx``（页面坐标系 px/s）。"""
    if not items:
        return
    with _motion_lock:
        fresh: dict[str, tuple[float, float, float, str]] = {}
        global_vx = 0.0
        for item in items:
            key = str(item.get("i") or "") or f"{item.get('x')},{item.get('y')}"
            text = str(item.get("t") or "")
            x = float(item.get("x") or 0)
            vx = 0.0
            prev = _motion.get(key)
            # 播放器会「池化复用」弹幕节点：同一个 id 换了文字就是另一条弹幕，
            # 不能拿旧的速度（否则会算出一个巨大的跳变）。
            if prev is not None and prev[3] == text:
                prev_x, prev_ts, prev_vx, _ = prev
                dt = now - prev_ts
                if MOTION_MIN_DT <= dt <= MOTION_MAX_AGE:
                    raw = (x - prev_x) / dt
                    if abs(raw) <= MOTION_MAX_VX:
                        vx = raw if abs(prev_vx) < 1e-6 else prev_vx + MOTION_SMOOTH * (raw - prev_vx)
            if vx:
                item["vx"] = round(vx, 1)
            fresh[key] = (x, now, vx, text)
        # 滚动弹幕速度基本一致：新进场的弹幕（贴着右边缘）可以直接套用整体速度，
        # 免得第一条 60ms 因为还没有速度估计而「顿」一下。
        speeds = [entry[2] for entry in fresh.values() if abs(entry[2]) >= MOTION_MIN_VX]
        if speeds:
            global_vx = sum(speeds) / len(speeds)
        if global_vx:
            width = float(_state.get("viewport_width") or 0)
            for item in items:
                if abs(float(item.get("vx") or 0)) >= MOTION_MIN_VX or not width:
                    continue
                right = float(item.get("x") or 0) + float(item.get("w") or 0)
                if right >= width - MOTION_EDGE_SLACK:
                    item["vx"] = round(global_vx, 1)
        _motion.clear()
        _motion.update(fresh)

def _reset_motion() -> None:
    """清空速度样本（停映射 / 换页时调用，避免用到过期数据）。"""
    with _motion_lock:
        _motion.clear()

# 游戏窗口识别：与 config.py 的 'windows' 段保持一致
# （hwnd_class = 'UnrealWindow'，exe = 'Client-Win64-Shipping.exe'）。
#
# ⚠️ 只按类名匹配是不够的：**所有虚幻引擎游戏**的窗口类名都是 UnrealWindow。
# 实测本机就开着另一个 UE 游戏（Abiotic Factor，窗口 2560x1440），
# 单靠类名会把弹幕锚到它上面去。所以必须再核对进程 exe 名。
GAME_HWND_CLASS = "UnrealWindow"
GAME_PROCESS_EXES = ("client-win64-shipping.exe",)
_GAME_MIN_WIDTH = 640
_GAME_MIN_HEIGHT = 360
_GAME_LOOKUP_INTERVAL = 2.0

def _send(status_queue, kind: str, payload: Any) -> None:
    try:
        status_queue.put((kind, payload))
    except Exception:
        pass

def _trace(message: str) -> None:
    """把诊断信息写到 stderr（主进程会收集到日志里）。

    受悬浮浏览器的日志总开关控制：开关关掉时这里什么都不写。
    """
    fb_log.trace(message)

def _coerce_handle(value) -> int:
    """把各种「窗口句柄」表示统一成 int。

    pywebview 在 Windows 上返回的是 pythonnet 的 ``IntPtr``（不是 int），
    也有后端直接给 int。两种都要能吃下。
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return int(value)
    # IntPtr / ctypes 句柄：优先 ToInt64，其次 int()
    for attribute in ("ToInt64", "ToInt32"):
        method = getattr(value, attribute, None)
        if callable(method):
            try:
                return int(method())
            except Exception:
                continue
    try:
        return int(value)
    except Exception:
        return 0

def _resolve_hwnd(window) -> int:
    """解析 pywebview 窗口在 Windows 上的 HWND。"""
    if window is None:
        return 0
    for attribute in ("handle", "hwnd"):
        hwnd = _coerce_handle(getattr(window, attribute, None))
        if hwnd:
            return hwnd
    native = getattr(window, "native", None)
    for attribute in ("Handle", "handle", "hwnd"):
        hwnd = _coerce_handle(getattr(native, attribute, None))
        if hwnd:
            return hwnd
    return 0

def _apply_window_style(window, opacity: float, on_top: bool) -> int:
    """应用置顶与透明度，返回窗口句柄。

    注意：**不要**在这里手动去掉 ``WS_CAPTION`` / ``WS_THICKFRAME`` 等样式。
    无边框已经由 pywebview 的 ``frameless=True``（内部 ``FormBorderStyle=None``）
    处理；再次手动 ``SetWindowLongW`` 去掉这些样式会破坏 WinForms 内部的
    样式管理，导致窗口 ``Show`` 后仍然不可见（实测 ``IsWindowVisible``
    恒为 False——这正是「窗口闪现一下就消失」的根因）。
    """
    if os.name != "nt":
        return 0

    hwnd = 0
    for _ in range(30):
        hwnd = _resolve_hwnd(window)
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        return 0

    _state["opacity"] = float(opacity)
    _state["on_top"] = bool(on_top)

    # 扩展样式（toolwindow / layered / transparent）与置顶统一在这里管理
    _sync_window_ex_style()
    return hwnd

def _effective_opacity() -> float:
    """计算当前应使用的透明度。

    穿透模式下，鼠标悬停在窗口内时降低到 ``hover_opacity``，便于看清下层内容；
    其余时候用正常 ``opacity``。
    """
    if _state.get("click_through") and _state.get("hovering"):
        return float(_state.get("hover_opacity", 0.3))
    return float(_state.get("opacity", 0.9))

def _sync_window_ex_style() -> None:
    """统一管理扩展样式（toolwindow / layered / transparent）+ 置顶 + 透明度。

    核心原则：**WS_EX_LAYERED 只在需要半透明（opacity < 1.0）时才设置**。
    对一个完全不透明的窗口动态添加分层样式，会把渲染路径从普通 GDI 切到
    layered，部分 Windows/显卡驱动下窗口会因此渲染成透明——表现就是
    「窗口闪现一下就消失」（其实窗口还在，只是看不见了）。
    """
    if os.name != "nt":
        return

    hwnd = _state.get("hwnd") or 0
    if not hwnd:
        return

    user32 = ctypes.windll.user32
    opacity = _effective_opacity()
    on_top = bool(_state.get("on_top", True))
    click_through = bool(_state.get("click_through", False))
    need_layered = opacity < 1.0

    try:
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ex_style |= WS_EX_TOOLWINDOW
        if need_layered:
            ex_style |= WS_EX_LAYERED
        else:
            ex_style &= ~WS_EX_LAYERED
        if click_through:
            ex_style |= WS_EX_TRANSPARENT
        else:
            ex_style &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)

        user32.SetWindowPos(
            hwnd, HWND_TOPMOST if on_top else HWND_NOTOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )

        # 只有真的处于分层状态才设置 alpha；alpha=255 的 layered 窗口
        # 在部分环境下仍可能渲染异常，因此完全透明需求之外一律不走 layered。
        if need_layered:
            user32.SetLayeredWindowAttributes(hwnd, 0, int(opacity * 255), LWA_ALPHA)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 鼠标穿透 / 拖动 / 缩放
# ---------------------------------------------------------------------------
def _overlay():
    """懒创建弹幕覆盖层（第一次开启镜像时才建窗口）。"""
    overlay = _state.get("overlay")
    if overlay is not None:
        return overlay
    if os.name != "nt":
        return None
    overlay = DanmakuOverlay()
    if not overlay.start():
        _trace(f"弹幕覆盖层创建失败: {overlay.last_error}")
        return None
    _state["overlay"] = overlay
    _sync_overlay_geometry()
    return overlay

def _subtitle_overlay():
    """懒创建字幕层（第一次开字幕时才建窗口）。"""
    overlay = _state.get("subtitle_overlay")
    if overlay is not None and overlay.running:
        return overlay
    if os.name != "nt":
        return None
    overlay = SubtitleOverlay()
    if not overlay.start():
        _trace(f"字幕层创建失败: {overlay.last_error}")
        _state["subtitle_overlay"] = None
        return None
    _state["subtitle_overlay"] = overlay
    _sync_overlay_settings()
    _sync_overlay_geometry()
    return overlay


def _sync_overlay_geometry() -> None:
    """把覆盖层放到该去的地方（见 ``_overlay_rect``）。

    默认锚到**游戏画面**左上角、尺寸仍为原视频窗口大小（1:1 照搬页面坐标）；
    游戏没开时退回跟随悬浮窗。
    """
    overlay = _state.get("overlay")
    if overlay is None:
        return
    rect = _overlay_rect()
    if rect is None:
        return
    overlay.set_geometry(*rect)
    subtitle = _state.get("subtitle_overlay")
    if subtitle is not None:
        subtitle.set_geometry(*rect)

def _apply_mirror() -> None:
    """根据「弹幕 / 字幕」两个开关的当前状态，启停覆盖层与页面采集。

    两个开关独立：开哪个就只画哪个；都关就隐藏覆盖层并停掉页面采集。
    """
    danmaku = bool(_state.get("mirror_danmaku"))
    subtitle = bool(_state.get("mirror_subtitle"))
    on = danmaku or subtitle
    window = _state.get("window")
    if not on:
        _state["mirror_on"] = False
        _reset_motion()
        overlay = _state.get("overlay")
        if overlay is not None:
            overlay.set_content([], None)
            overlay.set_engine_enabled(False)
            overlay.set_visible(False)
        subtitle_layer = _state.get("subtitle_overlay")
        if subtitle_layer is not None:
            subtitle_layer.set_visible(False)
        if window is not None:
            _evaluate(window, "window.__okMirrorSet && window.__okMirrorSet(false);")
        return
    overlay = _overlay()
    if overlay is None:
        _state["mirror_on"] = False
        return
    _state["mirror_on"] = True
    _sync_overlay_geometry()
    # 这一段视频的弹幕如果已经拉好了，直接继续用自绘模式（引擎里还留着数据）
    overlay.set_engine_enabled(
        danmaku and _state.get("danmaku_source") == "engine"
        and int(_state.get("danmaku_items") or 0) > 0
    )
    overlay.set_visible(True)
    _sync_overlay_settings()
    if subtitle:
        subtitle_layer = _subtitle_overlay()
        if subtitle_layer is not None:
            subtitle_layer.set_visible(True)
    if window is not None:
        _evaluate(window, "window.__okMirrorSet && window.__okMirrorSet(true);")
        _notify_danmaku_source(window)
        _push_settings_to_page(window)
        _sync_toolbar_state(window)

def _set_mirror(on: bool) -> None:
    """总开关（工具条按钮 / 热键用）：同时开/关弹幕和字幕两个子开关。"""
    on = bool(on)
    _state["mirror_danmaku"] = on
    _state["mirror_subtitle"] = on
    _apply_mirror()

# ---------------------------------------------------------------- 自绘模式
#
# 目标：**不抄页面的 DOM，自己拉弹幕数据自己算位置**。
# 页面只负责上报「现在播到第几秒、哪个视频」（每 200ms 一次），
# 弹幕由子进程按 cid 拉取（见 bilibili_danmaku），位置由引擎按播放时刻算出
# （见 danmaku_engine）——于是 seek / 倍速天然正确，也不存在采样间隔导致的跳帧。
#
# 拉不到数据（番剧要登录、被风控、非视频页…）就自动退回「抄 DOM」的老路径。

def _on_media(data: dict) -> None:
    """页面每 200ms 上报一次播放进度：更新时钟，并在需要时触发弹幕拉取。"""
    overlay = _state.get("overlay")
    if overlay is None or not _state.get("mirror_on"):
        return
    # 离线调试模式：内容与时钟都由调试接口直接给，别被页面上报覆盖
    if _state.get("danmaku_debug"):
        return
    window = _state.get("window")
    # 后台线程不允许直接碰页面，数据源变化在这里（JS 桥线程）补一次通知
    if _state.pop("pending_source_notify", False):
        _notify_danmaku_source(window)

    seconds = float(data.get("t") or 0.0)
    rate = float(data.get("rate") or 1.0) or 1.0
    paused = bool(data.get("paused"))
    previous = float(_state.get("media_t") or 0.0)
    if not _state.get("media_paused") and abs(seconds - previous) > 2.0:
        _state["media_seeks"] = int(_state.get("media_seeks") or 0) + 1
    _state["media_t"] = seconds
    _state["media_paused"] = paused
    _state["media_rate"] = rate
    overlay.set_playback(seconds, rate, paused)

    if not _state.get("mirror_danmaku"):
        return
    _ensure_danmaku(data)

def _ensure_danmaku(data: dict) -> None:
    """按需拉取当前视频的弹幕（放后台线程，别堵住消息循环）。"""
    cid = int(data.get("cid") or 0)
    bvid = str(data.get("bvid") or "").strip()
    page = int(data.get("p") or 1)
    if not cid and not bvid:
        if _state.get("danmaku_source") != "dom":
            _set_source("dom", "页面给不出视频标识")
        return
    if cid and cid == int(_state.get("danmaku_cid") or 0):
        return
    if cid and cid in _state.get("danmaku_failed", set()):
        return
    with _danmaku_lock:
        if _state.get("danmaku_loading"):
            return
        _state["danmaku_loading"] = True
    threading.Thread(target=_load_danmaku, args=(cid, bvid, page),
                     name="DanmakuFetch", daemon=True).start()

def _load_danmaku(cid: int, bvid: str, page: int) -> None:
    """后台线程：拉整段弹幕 → 交给覆盖层的引擎 → 切到自绘模式。"""
    try:
        if not cid:
            cid = bilibili_danmaku.resolve_cid(bvid, page)
        if not cid:
            _set_source("dom", f"拿不到 cid（bvid={bvid!r} p={page}）")
            return
        items = bilibili_danmaku.fetch(cid)
        if not items:
            _state.setdefault("danmaku_failed", set()).add(cid)
            _set_source("dom", f"cid={cid} 拉不到弹幕")
            return
        overlay = _state.get("overlay")
        if overlay is None:
            return
        overlay.load_danmaku(items)
        _state["danmaku_cid"] = cid
        _state["danmaku_items"] = len(items)
        _set_source("engine", f"cid={cid} / {len(items)} 条")
    except Exception as error:  # pragma: no cover - 兜底
        fb_log.warning(f"弹幕拉取线程异常: {error}")
        _set_source("dom", f"异常 {error!r}")
    finally:
        _state["danmaku_loading"] = False

def _set_source(source: str, reason: str = "") -> None:
    """切换「弹幕数据从哪来」：``engine`` = 自己算位置，``dom`` = 抄页面坐标。

    可能在后台线程被调用，所以这里只改状态 + 动覆盖层（线程安全），
    「通知页面停采弹幕 DOM」留给 JS 桥线程的 ``_on_media`` 去做。
    """
    source = "engine" if source == "engine" else "dom"
    previous = _state.get("danmaku_source")
    _state["danmaku_source"] = source
    overlay = _state.get("overlay")
    if overlay is not None:
        overlay.set_engine_enabled(source == "engine" and bool(_state.get("mirror_danmaku")))
    _state["pending_source_notify"] = True
    if previous != source:
        fb_log.info(f"弹幕数据源: {previous} -> {source}（{reason}）")

def _notify_danmaku_source(window) -> None:
    """告诉页面现在用哪种数据源（engine 模式下页面可以省掉读弹幕 DOM）。"""
    if window is None:
        return
    mode = "engine" if _state.get("danmaku_source") == "engine" else "dom"
    _evaluate(window, f"window.__okSetDanmakuSource && window.__okSetDanmakuSource('{mode}');")

def _reset_danmaku() -> None:
    """换页 / 换视频：丢掉上一段视频的弹幕，回到「等新数据」的状态。"""
    overlay = _state.get("overlay")
    if overlay is not None:
        overlay.load_danmaku([])
        overlay.set_engine_enabled(False)
    _state["danmaku_cid"] = 0
    _state["danmaku_items"] = 0
    _state["danmaku_source"] = "dom"
    _state["danmaku_failed"] = set()
    _state["danmaku_debug"] = False      # 换页后回到正常（非调试）链路
    _state["pending_source_notify"] = True

def _sync_overlay_settings() -> dict:
    """把设置应用到覆盖层：弹幕走引擎参数 + 整体 alpha，字幕走字幕层。"""
    danmaku = dict(_state.get("danmaku_settings") or DEFAULT_DANMAKU_SETTINGS)
    overlay = _state.get("overlay")
    applied: dict = {}
    if overlay is not None:
        overlay.set_opacity(danmaku.get("opacity", 100))
        applied = overlay.set_danmaku_options({
            "filter_scroll": danmaku.get("filter_scroll"),
            "filter_fixed": danmaku.get("filter_fixed"),
            "area": danmaku.get("area"),
            "font_scale": danmaku.get("font_scale"),
            "speed_plus": danmaku.get("speed_plus"),
        })
    subtitle_overlay = _state.get("subtitle_overlay")
    if subtitle_overlay is not None:
        subtitle_overlay.set_options(_state.get("subtitle_settings") or {})
    return applied

def _push_settings_to_page(window) -> None:
    """把当前设置推给页面，让设置面板显示的值与真正生效的值一致。"""
    if window is None:
        return
    for kind, key in (("danmaku", "danmaku_settings"), ("subtitle", "subtitle_settings")):
        settings = _state.get(key) or {}
        payload = json.dumps(settings, ensure_ascii=False)
        _evaluate(
            window,
            f"window.__okApplySettings && window.__okApplySettings('{kind}', {payload});",
        )

def _apply_overlay_settings(data: dict) -> None:
    """设置面板推来的改动：合并 -> 应用 -> 回推当前值。

    只接受默认值里存在的键，并交给引擎做范围收敛（例如速度会被夹到 0.5~2.0），
    回推过去的是**收敛后的值**，页面据此更新显示。
    """
    kind = "subtitle" if data.get("kind") == "subtitle" else "danmaku"
    is_subtitle = kind == "subtitle"
    key = "subtitle_settings" if is_subtitle else "danmaku_settings"
    defaults = DEFAULT_SUBTITLE_SETTINGS if is_subtitle else DEFAULT_DANMAKU_SETTINGS
    incoming = data.get("settings") or {}
    merged = dict(_state.get(key) or defaults)
    for name, value in incoming.items():
        if name in defaults and value is not None:
            merged[name] = value
    _state[key] = merged
    _sync_overlay_settings()
    _push_settings_to_page(_state.get("window"))
    fb_log.debug(f"{kind} 设置: {merged}")

def _set_click_through(enabled: bool) -> None:
    """切换鼠标穿透（``WS_EX_TRANSPARENT``）。"""
    if os.name != "nt":
        return

    hwnd = _state.get("hwnd") or 0
    if not hwnd:
        _state["click_through"] = bool(enabled)
        return
    _state["click_through"] = bool(enabled)
    _sync_window_ex_style()

def _set_interactive(on: bool) -> None:
    """临时让窗口可被点击（穿透模式下需要点「恢复」时使用）。"""
    if os.name != "nt":
        return

    hwnd = _state.get("hwnd") or 0
    if not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if on:
            ex_style &= ~WS_EX_TRANSPARENT
        elif _state.get("click_through"):
            ex_style |= WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)
    except Exception:
        pass

def _get_window_rect():
    """读取窗口屏幕矩形（left/top/right/bottom）。"""
    if os.name != "nt" or ctypes is None:
        return None

    hwnd = _state.get("hwnd") or 0
    if not hwnd:
        return None
    try:
        rect = wintypes.RECT()
        if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return rect
    except Exception as error:
        _trace(f"_get_window_rect 失败: {error}")
    return None

def _is_window(hwnd: int) -> bool:
    """句柄是否仍是有效窗口。"""
    if os.name != "nt" or ctypes is None or not hwnd:
        return False
    try:
        return bool(_user32.IsWindow(hwnd))
    except Exception:
        return False

def _window_process_exe(hwnd: int) -> str:
    """窗口所属进程的可执行文件名（小写，失败返回空串）。"""
    if os.name != "nt" or ctypes is None or not hwnd:
        return ""
    handle = None
    try:
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        handle = _kernel32.OpenProcess(0x1000, 0, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
    except Exception as error:
        _trace(f"读取窗口进程名失败: {error}")
        return ""
    finally:
        if handle:
            try:
                _kernel32.CloseHandle(handle)
            except Exception:
                pass

def _find_game_hwnd() -> int:
    """按窗口类名 + 进程名找到「游戏窗口」，返回客户区面积最大的那个（0 = 没找到）。

    识别依据与 ``config.py`` 的 ``windows`` 段一致：``hwnd_class='UnrealWindow'``
    且进程 exe 为 ``Client-Win64-Shipping.exe``（《鸣潮》）。进程名这道校验是必须的
    —— 所有虚幻引擎游戏的类名都叫 UnrealWindow，光看类名会认错游戏。
    再加一道尺寸门槛，避免把启动器 / 小提示窗当成游戏窗口。
    """
    if os.name != "nt" or ctypes is None:
        return 0

    candidates: list[tuple[int, int]] = []

    def _visit(hwnd, _lparam):
        buffer = ctypes.create_unicode_buffer(256)
        if not _user32.GetClassNameW(hwnd, buffer, 256):
            return 1
        if buffer.value != GAME_HWND_CLASS or not _user32.IsWindowVisible(hwnd):
            return 1
        if _window_process_exe(int(hwnd)) not in GAME_PROCESS_EXES:
            return 1
        rect = wintypes.RECT()
        if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return 1
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width >= _GAME_MIN_WIDTH and height >= _GAME_MIN_HEIGHT:
            candidates.append((width * height, int(hwnd)))
        return 1

    try:
        callback = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(_visit)
        _user32.EnumWindows(callback, None)
    except Exception as error:
        _trace(f"枚举游戏窗口失败: {error}")
        return 0
    return max(candidates)[1] if candidates else 0

def _client_rect_on_screen(hwnd: int):
    """窗口「客户区」（即游戏画面）在屏幕上的 ``[x, y, width, height]``。"""
    if os.name != "nt" or ctypes is None or not hwnd:
        return None
    try:
        rect = wintypes.RECT()
        if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        origin = wintypes.POINT(0, 0)
        if not _user32.ClientToScreen(hwnd, ctypes.byref(origin)):
            return None
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return None
        return [int(origin.x), int(origin.y), int(width), int(height)]
    except Exception as error:
        _trace(f"读取游戏画面矩形失败: {error}")
        return None

def _refresh_game_hwnd(force: bool = False) -> int:
    """刷新缓存的游戏窗口句柄（默认 2 秒内不重复枚举）。

    优先用主进程推来的 ``game_hwnd_hint``（ok 的 device_manager 已经解析过，
    更权威），它无效 / 拿不到时再自己按「类名 + 进程名」搜 —— 这样游戏后启动、
    或者独立跑子进程时也能自动发现。
    """
    now = time.time()
    cached = int(_state.get("game_hwnd") or 0)
    if not force and now - float(_state.get("game_hwnd_ts") or 0.0) < _GAME_LOOKUP_INTERVAL:
        if not cached or _is_window(cached):
            return cached

    hint = int(_state.get("game_hwnd_hint") or 0)
    hwnd = hint if _is_window(hint) else _find_game_hwnd()
    if hwnd != cached:
        _trace(f"游戏窗口句柄: {hwnd if hwnd else '未找到'}")
    _state["game_hwnd"] = hwnd
    _state["game_hwnd_ts"] = now
    return hwnd

def _first_font_family(css_family: str) -> str:
    """从 CSS ``font-family`` 列表里取第一个真实字体名。

    实测 B 站弹幕是 ``SimHei, "Microsoft JhengHei", Arial, Helvetica, sans-serif``，
    取到 ``SimHei`` 才能画得跟播放器里一样（而不是默认的微软雅黑）。
    """
    for chunk in str(css_family or "").split(","):
        name = chunk.strip().strip('"').strip("'")
        if not name:
            continue
        if name.lower() in ("sans-serif", "serif", "monospace", "system-ui", "cursive"):
            continue
        return name
    return ""

def _parse_mirror_style(style: Any) -> dict:
    """把页面采集到的 ``{ff, fw, fs, blur}`` 规整成覆盖层用的样式。"""
    if not isinstance(style, dict):
        return {}
    try:
        weight = int(float(str(style.get("fw") or 700).replace("bold", "700")))
    except Exception:
        weight = 700
    try:
        blur = float(style.get("blur") or 0)
    except Exception:
        blur = 0.0
    return {
        "family": _first_font_family(style.get("ff")),
        "bold": weight >= 600,
        # 站点没写 text-shadow（blur=0）时也给 1px：弹幕压在游戏画面上，
        # 一点黑边是「能看清」和「看不清」的区别。
        "blur": blur if blur > 0 else 1.0,
    }

def _overlay_rect():
    """覆盖层该放在哪：``(x, y, width, height)``。

    **尺寸 = 游戏窗口大小**：整块铺满游戏画面（客户区），弹幕的位置按
    ``覆盖层尺寸 ÷ 页面视口尺寸`` 映射进去（字号/样式不变），看起来就是
    「视频里的弹幕铺满整个游戏画面」。

    找不到游戏窗口（游戏没开）时退回「跟随悬浮窗」的老行为，此时比例恰好 1:1。
    """
    rect = _get_window_rect()
    if rect is None:
        return None
    game = _client_rect_on_screen(_refresh_game_hwnd())
    if game is not None:
        return (game[0], game[1], game[2], game[3])
    return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

def _restore_window(window) -> None:
    """从最小化状态恢复窗口。"""
    if os.name != "nt":
        return

    hwnd = _state.get("hwnd") or 0
    if not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    except Exception:
        pass

def _handle_drag_action(payload: dict) -> dict:
    """处理来自悬浮工具条的拖动 / 缩放请求，返回本轮几何结果。"""
    mode = str(payload.get("mode") or "move")
    action = str(payload.get("action") or "")
    try:
        sx = int(payload.get("sx") or 0)
        sy = int(payload.get("sy") or 0)
    except (TypeError, ValueError):
        sx = sy = 0

    key = f"drag_{mode}"
    pending = _state.get("pending_geometry")

    if action == "drag_start":
        rect = _get_window_rect()
        if rect is None:
            return {}
        _state[key] = {"sx": sx, "sy": sy,
                       "x": rect.left, "y": rect.top,
                       "w": rect.right - rect.left, "h": rect.bottom - rect.top}
        return {}

    origin = _state.get(key)
    if not origin:
        return {}

    if action == "drag_end":
        if pending:
            _state["pending_geometry"] = None
            return {"geometry": pending}
        return {}

    if os.name != "nt":
        return {}
    user32 = ctypes.windll.user32
    hwnd = _state.get("hwnd") or 0
    if not hwnd:
        return {}

    dx = sx - origin["sx"]
    dy = sy - origin["sy"]
    x, y, w, h = origin["x"], origin["y"], origin["w"], origin["h"]
    min_w, min_h = 240, 140
    if mode == "move":
        x, y = origin["x"] + dx, origin["y"] + dy
    elif mode == "e":
        w = max(min_w, origin["w"] + dx)
    elif mode == "w":
        x = origin["x"] + dx
        w = max(min_w, origin["w"] - dx)
    elif mode == "s":
        h = max(min_h, origin["h"] + dy)
    elif mode == "n":
        y = origin["y"] + dy
        h = max(min_h, origin["h"] - dy)
    elif mode == "se":
        w = max(min_w, origin["w"] + dx)
        h = max(min_h, origin["h"] + dy)
    elif mode == "sw":
        x = origin["x"] + dx
        w = max(min_w, origin["w"] - dx)
        h = max(min_h, origin["h"] + dy)
    elif mode == "ne":
        y = origin["y"] + dy
        w = max(min_w, origin["w"] + dx)
        h = max(min_h, origin["h"] - dy)
    elif mode == "nw":
        x = origin["x"] + dx
        y = origin["y"] + dy
        w = max(min_w, origin["w"] - dx)
        h = max(min_h, origin["h"] - dy)

    user32.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h),
                        SWP_NOZORDER | SWP_NOACTIVATE)
    geometry = [int(x), int(y), int(w), int(h)]
    _state["pending_geometry"] = geometry
    _sync_overlay_geometry()
    return {}

def _apply_alpha(opacity: float) -> None:
    if os.name != "nt":
        return

    hwnd = _state.get("hwnd") or 0
    if hwnd:
        try:
            ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0, int(opacity * 255), LWA_ALPHA)
        except Exception:
            pass

def _evaluate(window, script: str, timeout: float = 5.0):
    """执行页面 JS，并加超时保护。

    ``about:blank`` / 尚未就绪的页面上，``evaluate_js`` 可能长时间不返回
    （实测会一直挂住）。这里用短命线程包一层：超时就放弃这次求值，
    但求值线程本身是 daemon，不会拖住进程退出。
    """
    if window is None:
        return None
    box: dict[str, Any] = {}

    def _run():
        try:
            box["value"] = window.evaluate_js(script)
        except Exception as error:
            box["error"] = error

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        _trace(f"_evaluate 超时（>{timeout}s），脚本片段: {script[:60]!r}")
        return None
    return box.get("value")

def _poll_states(window, status_queue, stop_event) -> None:
    """周期性把视频状态回传给主进程。"""
    while not stop_event.is_set():
        try:
            payload = _evaluate(window, "window.__okStatus && window.__okStatus();")
            if payload is not None:
                _send(status_queue, "state", payload)
        except Exception:
            pass
        stop_event.wait(0.5)

def run_browser_process(config: dict, command_queue, status_queue) -> None:
    """子进程主函数：创建窗口、进入事件循环、处理指令。"""
    # 与父进程共用同一个日志开关（父进程通过启动配置传进来）
    fb_log.set_verbose(bool(config.get("verbose")))
    # 初始镜像状态（页面 loaded 之后真正生效）。两个开关各自独立。
    _state["mirror_danmaku"] = bool(config.get("mirror_danmaku") or config.get("mirror"))
    _state["mirror_subtitle"] = bool(config.get("mirror_subtitle") or config.get("mirror"))
    # 游戏窗口句柄提示（弹幕覆盖层的锚点；解析失败会自动退回跟随悬浮窗）
    _state["game_hwnd_hint"] = int(config.get("game_hwnd") or 0)
    try:
        import webview
    except Exception as error:
        _send(status_queue, "error", f"pywebview 不可用: {error}")
        return

    # 让 target="_blank" / window.open 的新窗口在当前悬浮窗内打开，
    # 而不是跳转到系统默认浏览器。pywebview 默认会把新窗口链接丢给系统浏览器，
    # 但悬浮浏览器要「像原生浏览器一样」在窗内导航，所以这里关掉外部跳转。
    try:
        webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = False
    except Exception:
        pass

    window_holder: dict[str, Any] = {"window": None}
    stop_event = threading.Event()

    class _ToolbarApi:
        """暴露给悬浮工具条的 JS 桥（``window.pywebview.api``）。"""

        def ui(self, payload=None):
            payload = payload or {}
            action = str(payload.get("action") or "")
            _state["bridge_alive"] = True
            _state["last_bridge_action"] = action
            try:
                if action == "query_state":
                    # 页面主动查询当前状态：用于刷新按钮外观与解除穿透
                    _sync_toolbar_state(window_holder.get("window"))
                    return {
                        "click_through": bool(_state.get("click_through")),
                        "on_top": bool(_state.get("on_top")),
                        "opacity": float(_state.get("opacity", 0.9)),
                        "mirror": bool(_state.get("mirror_on")),
                        "mirror_danmaku": bool(_state.get("mirror_danmaku")),
                        "mirror_subtitle": bool(_state.get("mirror_subtitle")),
                    }
                if action == "mirror":
                    # 主页面每 60ms 推一次「当前可见的弹幕/字幕」+ 站点样式 + 视口尺寸。
                    # 两个开关独立：关掉的那类不往覆盖层里画。
                    data = payload.get("data") or {}
                    overlay = _state.get("overlay")
                    if overlay is not None and _state.get("mirror_on"):
                        _state["viewport_width"] = float(data.get("vw") or 0)
                        overlay.set_viewport(data.get("vw") or 0, data.get("vh") or 0)
                        style = _parse_mirror_style(data.get("style"))
                        if style:
                            overlay.set_style(style)
                        subtitle = data.get("sub") if _state.get("mirror_subtitle") else None
                        # 字幕交给**字幕层**（独立的逐像素半透明窗口），
                        # 弹幕层只画弹幕 —— 这样字幕的大小/位置/背景三个设置才能生效。
                        subtitle_layer = _state.get("subtitle_overlay")
                        if subtitle_layer is not None:
                            subtitle_layer.set_content(subtitle, style or {})
                        if _state.get("danmaku_source") == "engine":
                            # 自绘模式：弹幕内容由引擎按播放时刻算，页面推来的坐标一律不用。
                            # 字幕没有接口可拿（要登录），所以继续走页面采集。
                            overlay.set_content([], None)
                        else:
                            danmaku = (list(data.get("dm") or [])
                                       if _state.get("mirror_danmaku") else [])
                            # 估算横向速度：覆盖层据此在两次推送之间本地补帧（60fps 平滑滚动）
                            if danmaku:
                                _attach_velocity(danmaku, time.time())
                            overlay.set_content(danmaku, None)
                    return True
                if action == "overlay_settings":
                    # 工具条上右键弹出的设置面板改了东西
                    _apply_overlay_settings(payload.get("data") or {})
                    return True
                if action == "media":
                    # 页面每 200ms 上报一次播放进度（自绘模式的唯一输入）
                    _on_media(payload.get("data") or {})
                    return True
                if action in ("toggle_mirror", "toggle_mirror_danmaku", "toggle_mirror_subtitle"):
                    if action == "toggle_mirror":
                        _set_mirror(not bool(_state.get("mirror_on")))
                    elif action == "toggle_mirror_danmaku":
                        _state["mirror_danmaku"] = not bool(_state.get("mirror_danmaku"))
                        _apply_mirror()
                    else:
                        _state["mirror_subtitle"] = not bool(_state.get("mirror_subtitle"))
                        _apply_mirror()
                    _send(status_queue, "ui", {
                        "mirror": bool(_state.get("mirror_on")),
                        "mirror_danmaku": bool(_state.get("mirror_danmaku")),
                        "mirror_subtitle": bool(_state.get("mirror_subtitle")),
                    })
                    return True
                if action in ("drag_start", "drag_move", "drag_end"):
                    result = _handle_drag_action(payload)
                    if result.get("geometry"):
                        _send(status_queue, "geometry", result["geometry"])
                    return True
                if action == "opacity":
                    value = max(20, min(100, int(float(payload.get("value") or 90))))
                    _state["opacity"] = value / 100.0
                    _apply_alpha(_state["opacity"])
                    _send(status_queue, "ui", {"opacity": value / 100.0})
                    return True
                if action == "toggle_click_through":
                    target = not bool(_state.get("click_through"))
                    _set_click_through(target)
                    _sync_toolbar_state(window_holder.get("window"))
                    _send(status_queue, "ui", {"click_through": target})
                    return target
                if action == "toggle_on_top":
                    target = not bool(_state.get("on_top"))
                    _state["on_top"] = target
                    _apply_top(target)
                    _sync_toolbar_state(window_holder.get("window"))
                    _send(status_queue, "ui", {"on_top": target})
                    return target
                if action == "hide":
                    window = window_holder["window"]
                    if window is not None:
                        window.hide()
                        _state["visible"] = False
                    return True
                if action == "close":
                    # 关闭按钮：销毁窗口，webview.start() 随即返回并通知主进程清理
                    _send(status_queue, "ui", {"close": True})
                    window = window_holder["window"]
                    if window is not None:
                        window.destroy()
                    return True
            except Exception as error:
                _send(status_queue, "error", f"工具条动作 {action} 失败: {error}")
            return False

    api = _ToolbarApi()
    init_lock = threading.Lock()

    def _on_page_loaded():
        """页面导航完成时回调：此时 JS 桥已注入、页面可执行脚本。

        脚本注入必须放在这里，而不是窗口刚创建时——WebView2 在导航完成前
        ``CoreWebView2`` 尚未就绪，过早调用 ``evaluate_js`` 会触发
        NullReferenceException，在系统资源紧张（游戏占用 GPU）时甚至导致
        WebView2 崩溃、窗口闪退。
        """
        window = window_holder.get("window")
        if window is not None:
            _install_scripts(window)
            # 换页/换视频后先清掉上一段视频的弹幕，等新的上报到齐再切回自绘
            _reset_danmaku()
            # 换页/跳转后把镜像状态恢复回去（新页面的脚本是刚注入的）
            _apply_mirror()

    def _on_shown():
        """窗口真正显示后回调：补同步一次扩展样式。

        窗口句柄在 ``Show()`` 之前就能解析到，那时设置的 ``WS_EX_TOOLWINDOW``
        等会被 WinForms 的显示流程重置；在这里（Shown 事件后）再同步一次，
        确保「不出现在任务栏」等扩展样式最终生效。

        """
        _sync_window_ex_style()

    def _maybe_init(window) -> bool:
        """首次完成窗口初始化（样式/句柄/轮询线程）。幂等。

        只做与页面无关的初始化：解析句柄、置顶、透明度、起轮询线程。
        脚本注入交给 ``window.events.loaded``（导航完成）与看门狗，避免在
        WebView2 未就绪时执行 JS。
        """
        with init_lock:
            if _state.get("initialized"):
                return False
            _state["initialized"] = True

        if window is None:
            with init_lock:
                _state["initialized"] = False
            return False
        opacity = float(config.get("opacity", 0.9))
        on_top = bool(config.get("on_top", True))
        hwnd = _apply_window_style(window, opacity, on_top)
        _state["hwnd"] = hwnd
        _state["opacity"] = opacity
        _state["on_top"] = on_top
        _state["hover_opacity"] = float(config.get("hover_opacity", 0.3))
        # 窗口显示后补同步扩展样式；页面每次导航完成都重新注入脚本
        try:
            window.events.shown += _on_shown
            window.events.loaded += _on_page_loaded
        except Exception:
            pass
        # 先把轮询线程拉起来：几何/状态/悬停同步不依赖页面是否可执行 JS
        for target in (_poll_states, _poll_geometry, _poll_toolbar, _poll_hover):
            threading.Thread(
                target=target, args=(window, status_queue, stop_event), daemon=True
            ).start()
        _send(status_queue, "ready", True)
        return True

    def on_loaded():
        window = window_holder.get("window")
        _maybe_init(window)

    def _startup_watchdog():
        """兜底：某些页面（如 about:blank）不触发 loaded 事件，
        这里保证初始化与 ready 一定发生，程序不会卡在「启动中」。"""
        stop_event.wait(6.0)
        if stop_event.is_set():
            return
        _maybe_init(window_holder.get("window"))

    try:
        window = webview.create_window(
            title="OK-WW 悬浮浏览器",
            url=config.get("url") or "about:blank",
            width=int(config.get("width", 560)),
            height=int(config.get("height", 340)),
            x=config.get("x"),
            y=config.get("y"),
            frameless=True,
            easy_drag=False,
            on_top=bool(config.get("on_top", True)),
            background_color="#101216",
            min_size=(240, 140),
            js_api=api,
        )
        window_holder["window"] = window
        _state["window"] = window
    except Exception as error:
        _send(status_queue, "error", f"创建窗口失败: {error}")
        return

    def command_worker():
        """处理来自主进程的指令。"""
        window = window_holder["window"]
        while not stop_event.is_set():
            try:
                command, argument = command_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            except Exception:
                break

            try:
                if command == "quit":
                    break
                elif command == "set_size":
                    width, height = argument
                    window.resize(int(width), int(height))
                    _sync_overlay_geometry()
                elif command == "set_opacity":
                    _state["opacity"] = float(argument)
                    _apply_alpha(argument)
                elif command == "set_hover_opacity":
                    _state["hover_opacity"] = float(argument)
                    _sync_window_ex_style()
                elif command == "set_position":
                    window.move(int(argument[0]), int(argument[1]))
                    _sync_overlay_geometry()
                elif command == "set_geometry":
                    x, y, width, height = argument
                    window.move(int(x), int(y))
                    window.resize(int(width), int(height))
                    _sync_overlay_geometry()
                elif command == "set_game_hwnd":
                    # 主进程（ok 的 device_manager）推来的游戏窗口句柄，作为类名搜索的兜底
                    _state["game_hwnd_hint"] = int(argument or 0)
                    _refresh_game_hwnd(force=True)
                    _sync_overlay_geometry()
                elif command == "set_mirror":
                    _set_mirror(bool(argument))
                elif command == "set_mirror_danmaku":
                    _state["mirror_danmaku"] = bool(argument)
                    _apply_mirror()
                elif command == "set_mirror_subtitle":
                    _state["mirror_subtitle"] = bool(argument)
                    _apply_mirror()
                elif command == "set_click_through":
                    _set_click_through(bool(argument))
                    _sync_toolbar_state(window)
                elif command == "set_interactive":
                    _set_interactive(bool(argument))
                elif command == "set_on_top":
                    _state["on_top"] = bool(argument)
                    _apply_top(bool(argument))
                elif command == "load_url":
                    window.load_url(argument)
                elif command == "show":
                    window.show()
                    _state["visible"] = True
                elif command == "hide":
                    window.hide()
                    _state["visible"] = False
                elif command == "toggle_visible":
                    # 穿透状态下无法点击悬浮窗，显示时顺手恢复交互
                    _state["click_through"] = False
                    _sync_window_ex_style()
                    _sync_toolbar_state(window)
                    # 用 _state["visible"] 而非 window.hidden：后者是构造时初值，
                    # 不会随 hide()/show() 更新，会导致「只能隐藏、无法再显示」。
                    if _state.get("visible"):
                        window.hide()
                        _state["visible"] = False
                    else:
                        window.show()
                        _restore_window(window)
                        _state["visible"] = True
                elif command == "refresh":
                    _install_scripts(window)
                    payload = _evaluate(window, "window.__okStatus && window.__okStatus();")
                    _send(status_queue, "state", payload)
                elif command == "eval_js":
                    request_id, script = argument
                    _send(status_queue, "eval_result", (request_id, _evaluate(window, script)))
                elif command == "inspect_state":
                    request_id = argument
                    _send(status_queue, "inspect_result", (request_id, {
                        "hwnd": int(_state.get("hwnd") or 0),
                        "click_through": bool(_state.get("click_through")),
                        "on_top": bool(_state.get("on_top")),
                        "opacity": float(_state.get("opacity", 0.9)),
                        "initialized": bool(_state.get("initialized")),
                    }))
                elif command == "inspect_mirror":
                    request_id = argument
                    overlay = _state.get("overlay")
                    window_rect = _get_window_rect()
                    game_hwnd = int(_state.get("game_hwnd") or 0)
                    info = {
                        "on": bool(_state.get("mirror_on")),
                        "danmaku": bool(_state.get("mirror_danmaku")),
                        "subtitle": bool(_state.get("mirror_subtitle")),
                        # 弹幕数据来源：engine = 自己拉数据自己算位置；dom = 抄页面坐标
                        "source": _state.get("danmaku_source"),
                        "danmaku_cid": int(_state.get("danmaku_cid") or 0),
                        "danmaku_items": int(_state.get("danmaku_items") or 0),
                        "danmaku_loading": bool(_state.get("danmaku_loading")),
                        "danmaku_failed": sorted(int(c) for c in _state.get("danmaku_failed") or []),
                        "media": {"t": round(float(_state.get("media_t") or 0.0), 3),
                                  "rate": float(_state.get("media_rate") or 1.0),
                                  "paused": bool(_state.get("media_paused")),
                                  "seeks": int(_state.get("media_seeks") or 0)},
                        "game_hwnd": game_hwnd,
                        "game_hint": int(_state.get("game_hwnd_hint") or 0),
                        "game_rect": _client_rect_on_screen(game_hwnd),
                        "window_rect": ([window_rect.left, window_rect.top,
                                         window_rect.right - window_rect.left,
                                         window_rect.bottom - window_rect.top]
                                        if window_rect is not None else None),
                        "overlay": overlay.debug_info() if overlay is not None else None,
                        "subtitle_layer": (subtitle_layer.debug_info()
                                           if (subtitle_layer := _state.get("subtitle_overlay"))
                                           else None),
                    }
                    _send(status_queue, "inspect_result", (request_id, info))
                elif command == "debug_load_danmaku":
                    # 离线调试/测试用：跳过网络，直接往引擎里灌一段弹幕。
                    # （回归测试靠它验证自绘渲染，不用依赖 B 站接口可用。）
                    overlay = _state.get("overlay")
                    if overlay is not None:
                        items = argument if isinstance(argument, list) else []
                        _state["danmaku_debug"] = True
                        overlay.load_danmaku(items)
                        _state["danmaku_items"] = len(items)
                        _state["danmaku_source"] = "engine" if items else "dom"
                        overlay.set_engine_enabled(
                            bool(items) and bool(_state.get("mirror_danmaku")))
                elif command == "debug_set_settings":
                    if isinstance(argument, dict):
                        _apply_overlay_settings(argument)
                elif command == "debug_set_playback":
                    # 离线调试/测试用：直接设定播放时刻（不走页面上报）
                    overlay = _state.get("overlay")
                    if overlay is not None:
                        values = list(argument or [])
                        seconds = float(values[0]) if values else 0.0
                        rate = float(values[1]) if len(values) > 1 else 1.0
                        paused = bool(values[2]) if len(values) > 2 else False
                        overlay.set_playback(seconds, rate, paused)
                        _state["media_t"] = seconds
                        _state["media_rate"] = rate
                        _state["media_paused"] = paused
                elif command == "get_geometry":
                    request_id = argument
                    rect = _get_window_rect()
                    if rect is None:
                        _send(status_queue, "inspect_result", (request_id, None))
                    else:
                        _send(status_queue, "inspect_result", (request_id, [
                            rect.left, rect.top,
                            rect.right - rect.left, rect.bottom - rect.top,
                        ]))
                elif command == "play_pause":
                    _send(status_queue, "state", _evaluate(window, "window.__okPlayPause && window.__okPlayPause();"))
                elif command == "seek":
                    value = json.dumps(float(argument))
                    _send(status_queue, "state", _evaluate(window, f"window.__okSeek && window.__okSeek({value});"))
                elif command == "set_rate":
                    value = json.dumps(float(argument))
                    _send(status_queue, "state", _evaluate(window, f"window.__okSetRate && window.__okSetRate({value});"))
                elif command == "hold_rate":
                    value = json.dumps(float(argument))
                    _send(status_queue, "state", _evaluate(window, f"window.__okHoldRate && window.__okHoldRate({value});"))
                elif command == "release_hold":
                    _send(status_queue, "state", _evaluate(window, "window.__okReleaseHold && window.__okReleaseHold();"))
                elif command in ("speed_up", "slow_down"):
                    _handle_rate(window, status_queue, command)
                elif command == "set_volume":
                    value = json.dumps(float(argument))
                    _send(status_queue, "state", _evaluate(window, f"window.__okSetVolume && window.__okSetVolume({value});"))
                elif command in ("volume_up", "volume_down"):
                    _handle_volume(window, status_queue, float(argument or 0.1), command)
                elif command == "toggle_mute":
                    _send(status_queue, "state", _evaluate(window, "window.__okToggleMute && window.__okToggleMute();"))
            except Exception as error:
                _send(status_queue, "error", f"处理指令 {command} 失败: {error}")

        stop_event.set()
        overlay = _state.get("overlay")
        if overlay is not None:
            overlay.stop()
            _state["overlay"] = None
        subtitle_layer = _state.get("subtitle_overlay")
        if subtitle_layer is not None:
            subtitle_layer.stop()
            _state["subtitle_overlay"] = None
        try:
            window.destroy()
        except Exception:
            pass
        # 给 WebView2 留出回收子进程的时间
        time.sleep(0.3)

    worker = threading.Thread(target=command_worker, name="BrowserCommandWorker", daemon=True)
    worker.start()
    threading.Thread(target=_startup_watchdog, name="BrowserStartupWatchdog", daemon=True).start()

    try:
        webview.start(on_loaded, debug=False, private_mode=False)
    except Exception as error:
        _send(status_queue, "error", f"启动 WebView 失败: {error}")
    finally:
        stop_event.set()
        _send(status_queue, "closed", True)
def _install_scripts(window) -> None:
    """注入视频控制脚本与悬浮工具条。"""
    _evaluate(window, VIDEO_JS)
    _evaluate(window, CONTROL_BAR_JS)
    _evaluate(window, MIRROR_JS)

def _sync_toolbar_state(window) -> None:
    """把穿透 / 置顶 / 弹幕 / 字幕状态同步给工具条，用于刷新按钮外观。"""
    if window is None:
        return
    click_through = "true" if _state.get("click_through") else "false"
    on_top = "true" if _state.get("on_top") else "false"
    mirror_danmaku = "true" if _state.get("mirror_danmaku") else "false"
    mirror_subtitle = "true" if _state.get("mirror_subtitle") else "false"
    _evaluate(
        window,
        "window.__okSyncButtons && window.__okSyncButtons("
        f"{click_through}, {on_top}, {mirror_danmaku}, {mirror_subtitle});",
    )

def _poll_toolbar(window, status_queue, stop_event) -> None:
    """看门狗：页面跳转或 SPA 重渲染会冲掉工具条，这里定时补注入。"""
    while not stop_event.is_set():
        try:
            alive = _evaluate(window, "!!document.getElementById('__ok_xbar')")
            if alive is False:
                _evaluate(window, CONTROL_BAR_JS)
                _sync_toolbar_state(window)
        except Exception:
            pass
        stop_event.wait(2.0)

def _poll_geometry(window, status_queue, stop_event) -> None:
    """兜底：窗口几何被外部改变时同步回主进程。"""
    last = None
    while not stop_event.is_set():
        rect = _get_window_rect()
        if rect is not None:
            current = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
            # 首次拿到真实矩形也要上报，否则主进程一直停在 (-1, -1) 占位值
            if current != last:
                _send(status_queue, "geometry", list(current))
            last = current
        # 游戏窗口会被玩家移动 / 改分辨率 / 关闭，覆盖层要跟着重算位置。
        # 句柄解析自带 2 秒缓存，set_geometry 在几何不变时会直接返回，开销可忽略。
        if _state.get("mirror_on"):
            _sync_overlay_geometry()
        stop_event.wait(0.5)

def _poll_hover(window, status_queue, stop_event) -> None:
    """穿透模式下，检测鼠标是否悬停在窗口内，悬停时降低透明度。

    穿透时窗口是 ``WS_EX_TRANSPARENT``，鼠标事件直接落到下层，因此这里主动用
    ``GetCursorPos`` + ``GetWindowRect`` 判断鼠标是否在窗口矩形内。
    """
    if os.name != "nt" or ctypes is None:
        return
    hovering = False
    while not stop_event.is_set():
        inside = False
        if _state.get("click_through"):
            rect = _get_window_rect()
            if rect is not None:
                point = wintypes.POINT()
                if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
                    inside = (
                        rect.left <= point.x < rect.right
                        and rect.top <= point.y < rect.bottom
                    )
        if inside != hovering:
            hovering = inside
            _state["hovering"] = hovering
            _sync_window_ex_style()
        stop_event.wait(0.1)

def _apply_alpha(opacity: float) -> None:
    if os.name != "nt":
        return
    _state["opacity"] = float(opacity)
    _sync_window_ex_style()

def _apply_top(on_top: bool) -> None:
    if os.name != "nt":
        return
    _state["on_top"] = bool(on_top)
    _sync_window_ex_style()

def _handle_rate(window, status_queue, command: str) -> None:
    payload = _evaluate(window, "window.__okStatus && window.__okStatus();")
    current = 1.0
    if isinstance(payload, dict):
        try:
            current = float(payload.get("rate") or 1.0)
        except (TypeError, ValueError):
            current = 1.0
    target = current + 0.25 if command == "speed_up" else current - 0.25
    target = max(0.25, min(4.0, target))
    value = json.dumps(round(target, 2))
    _send(status_queue, "state", _evaluate(window, f"window.__okSetRate && window.__okSetRate({value});"))

def _handle_volume(window, status_queue, step: float, command: str) -> None:
    """在子进程内读取实际音量并增减，避免主进程缓存的状态有延迟。"""
    payload = _evaluate(window, "window.__okStatus && window.__okStatus();")
    current = 1.0
    if isinstance(payload, dict):
        try:
            current = float(payload.get("volume") or 1.0)
        except (TypeError, ValueError):
            current = 1.0
    delta = step if command == "volume_up" else -step
    target = max(0.0, min(1.0, current + delta))
    value = json.dumps(round(target, 2))
    _send(status_queue, "state", _evaluate(window, f"window.__okSetVolume && window.__okSetVolume({value});"))

if __name__ == "__main__":  # pragma: no cover
    # 允许单独调试：python -m src.gui.floating_browser.webview_process
    fb_log.info("该模块由 FloatingBrowser 主进程调用，不应直接运行。")
