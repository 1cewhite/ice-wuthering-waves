"""悬浮浏览器配置标签页。

这个页面只负责「配置」与「启停」：

- 设置悬浮窗打开的网址
- 启动 / 停止 / 显示隐藏悬浮浏览器
- 查看运行状态与当前窗口几何
- 配置各功能对应的全局快捷键

窗口尺寸、透明度、鼠标穿透、置顶等操作**全部放在悬浮窗自带的工具条上**，
直接用鼠标拖动即可完成，不再依赖这里的数字输入框。
播放控制同样只在悬浮窗与快捷键中完成。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QGridLayout, QVBoxLayout, QSpinBox, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CheckBox, FluentIcon, LineEdit, PrimaryPushButton,
    PushButton, StrongBodyLabel,
)

from ok.gui.util.app import show_info_bar
from ok.gui.widget.CustomTab import CustomTab

from src.gui.floating_browser import log as fb_log
from src.gui.floating_browser.browser import _format_time, normalize_url
from src.gui.floating_browser.hotkeys import HOTKEY_ACTIONS
from src.gui.floating_browser.service import (
    FLOATING_BROWSER_CONFIG,
    HOTKEY_CONFIG,
    FloatingBrowserService,
)


PRESET_SITES = [
    ("Bilibili", "https://www.bilibili.com/"),
    ("YouTube", "https://www.youtube.com/"),
]


class FloatingBrowserTab(CustomTab):
    """悬浮浏览器标签页。"""

    state_refreshed = Signal(object)
    # 后台线程（子进程状态读取）里不能直接操作 Qt UI，这里用信号把
    # 「弹提示」切回主线程，避免 "Cannot set parent, new parent is in a
    # different thread" 以及 ok 主窗口未响应。
    notice_requested = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.service = FloatingBrowserService.instance()
        self._last_running = False
        self._build_ui()
        self._load_config()
        self.service.add_state_listener(self._on_state_changed)
        self.service.add_ui_listener(self._on_ui_event)
        self.state_refreshed.connect(self._render_state)
        self.notice_requested.connect(self._show_notice)

        # 定时刷新播放状态与窗口几何，保证界面与控制结果同步
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._poll_state)
        self._timer.start()

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------
    def _build_ui(self):
        self.add_widget(self._build_source_card(), stretch=0)
        self.add_widget(self._build_control_card(), stretch=0)
        self.add_widget(self._build_hotkey_card(), stretch=0)
        self.add_widget(self._build_status_card(), stretch=0)

    def _build_source_card(self) -> QWidget:
        container = QWidget()
        layout = QGridLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        layout.addWidget(BodyLabel(self.tr("Video URL")), 0, 0)
        self.url_edit = LineEdit(container)
        self.url_edit.setPlaceholderText("https://www.bilibili.com/")
        self.url_edit.setClearButtonEnabled(True)
        layout.addWidget(self.url_edit, 0, 1, 1, 2)

        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)
        for name, url in PRESET_SITES:
            button = PushButton(self.tr(name), container)
            button.clicked.connect(lambda _c=False, u=url: self._use_preset(u))
            preset_row.addWidget(button)
        preset_row.addStretch(1)
        layout.addLayout(preset_row, 1, 1, 1, 2)
        return self.add_card(self.tr("Video source"), container)

    def _build_control_card(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        launch_row = QHBoxLayout()
        self.start_button = PrimaryPushButton(FluentIcon.PLAY, self.tr("Start floating browser"), container)
        self.start_button.clicked.connect(self._start)
        self.stop_button = PushButton(FluentIcon.CANCEL, self.tr("Stop"), container)
        self.stop_button.clicked.connect(self._stop)
        self.hide_button = PushButton(FluentIcon.VIEW, self.tr("Show / Hide"), container)
        self.hide_button.clicked.connect(self._toggle_visible)
        launch_row.addWidget(self.start_button)
        launch_row.addWidget(self.stop_button)
        launch_row.addWidget(self.hide_button)
        launch_row.addStretch(1)
        layout.addLayout(launch_row)

        hover_row = QHBoxLayout()
        hover_row.setSpacing(8)
        hover_label = BodyLabel(self.tr("Hover opacity (click-through)"), container)
        self.hover_opacity_spin = QSpinBox(container)
        self.hover_opacity_spin.setRange(5, 100)
        self.hover_opacity_spin.setSingleStep(5)
        self.hover_opacity_spin.setSuffix(" %")
        self.hover_opacity_spin.setToolTip(
            self.tr("In click-through mode the window drops to this opacity while the pointer hovers over it, so you can see what is underneath.")
        )
        self.hover_opacity_spin.valueChanged.connect(self._on_hover_opacity_changed)
        hover_row.addWidget(hover_label)
        hover_row.addWidget(self.hover_opacity_spin)
        hover_row.addStretch(1)
        layout.addLayout(hover_row)

        self.log_output_check = CheckBox(self.tr("Output floating browser logs"), container)
        self.log_output_check.setToolTip(
            self.tr("Master switch for all floating browser output in the console and log file"
                    "(including subprocess diagnostics and launcher messages). Off by default.")
            + "\n"
            + self.tr("On-screen notices (pop-ups, status bar) are not affected.")
        )
        self.log_output_check.clicked.connect(self._on_log_output_changed)
        layout.addWidget(self.log_output_check)

        hint = CaptionLabel(
            self.tr("Window size, opacity, click-through, always-on-top, and the danmaku/subtitle mirroring switches"
                    "are all on the toolbar at the top of the floating window: drag the left area to move it,"
                    "drag the right/bottom edge to resize."),
            container,
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return self.add_card(self.tr("Startup & window"), container)

    def _build_hotkey_card(self) -> QWidget:
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(10)

        container = QWidget(wrapper)
        layout = QGridLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)
        layout.setColumnStretch(1, 1)

        self.hotkey_edits: dict[str, LineEdit] = {}
        for row, (action, meta) in enumerate(HOTKEY_ACTIONS.items()):
            label = BodyLabel(self.tr(meta["label"]), container)
            label.setToolTip(self.tr(meta["description"]))
            edit = LineEdit(container)
            edit.setPlaceholderText(meta["default"])
            edit.setMinimumWidth(180)
            edit.setToolTip(
                self.tr("Format example: ctrl+shift+space; leave empty or use none to disable")
            )
            layout.addWidget(label, row, 0)
            layout.addWidget(edit, row, 1)
            self.hotkey_edits[action] = edit

        wrapper_layout.addWidget(container)

        hint = CaptionLabel(
            self.tr("After changing hotkeys, click Save settings and restart the floating browser."
                    "The default step for forward/backward is 5 seconds."),
            wrapper,
        )
        hint.setWordWrap(True)
        wrapper_layout.addWidget(hint)

        save_row = QHBoxLayout()
        self.save_button = PrimaryPushButton(FluentIcon.SAVE, self.tr("Save settings"), wrapper)
        self.save_button.clicked.connect(self._save_config)
        self.rebind_button = PushButton(FluentIcon.SYNC, self.tr("Reload hotkeys now"), wrapper)
        self.rebind_button.clicked.connect(self._reload_hotkeys)
        save_row.addWidget(self.save_button)
        save_row.addWidget(self.rebind_button)
        save_row.addStretch(1)
        wrapper_layout.addLayout(save_row)
        return self.add_card(self.tr("Global hotkeys"), wrapper)

    def _build_status_card(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.status_title = StrongBodyLabel(self.tr("Not running"), container)
        self.status_detail = BodyLabel("", container)
        self.status_detail.setWordWrap(True)
        layout.addWidget(self.status_title)
        layout.addWidget(self.status_detail)
        return self.add_card(self.tr("Status"), container)

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------
    @property
    def name(self):
        return self.tr("Floating browser")

    @property
    def icon(self):
        return FluentIcon.GLOBE

    # ------------------------------------------------------------------
    # 配置读写
    # ------------------------------------------------------------------
    def _load_config(self):
        config = self._read_global_config(FLOATING_BROWSER_CONFIG) or {}
        self.url_edit.setText(str(config.get("url", "https://www.bilibili.com/")))
        try:
            hover = float(config.get("hover_opacity", 0.3))
        except (TypeError, ValueError):
            hover = 0.3
        self.hover_opacity_spin.setValue(int(round(hover * 100)))

        # 日志总开关：打开设置页就按配置应用一次（改完即时生效）
        log_output = bool(config.get("log_output", False))
        self.log_output_check.setChecked(log_output)
        self.service.set_verbose(log_output)

        hotkeys = self._read_global_config(HOTKEY_CONFIG) or {}
        for action, meta in HOTKEY_ACTIONS.items():
            edit = self.hotkey_edits.get(action)
            if edit is not None:
                edit.setText(str(hotkeys.get(action, meta["default"])))

    def _read_global_config(self, name: str):
        try:
            from ok import og

            return og.executor.global_config.get_config(name)
        except Exception as error:
            fb_log.debug(f"Failed to read config {name}: {error}")
            return None

    def _save_config(self):
        try:
            from ok import og

            browser_config = og.executor.global_config.get_config(FLOATING_BROWSER_CONFIG)
            browser_config["url"] = self.url_edit.text().strip() or "https://www.bilibili.com/"
            browser_config["hover_opacity"] = round(self.hover_opacity_spin.value() / 100.0, 2)
            browser_config["log_output"] = bool(self.log_output_check.isChecked())
            if hasattr(browser_config, "save_file"):
                browser_config.save_file()

            hotkey_config = og.executor.global_config.get_config(HOTKEY_CONFIG)
            for action, edit in self.hotkey_edits.items():
                hotkey_config[action] = edit.text().strip()
            if hasattr(hotkey_config, "save_file"):
                hotkey_config.save_file()
            self._persist_window_geometry()
            show_info_bar(self.window(), self.tr("Settings saved"), title=self.tr("Success"))
        except Exception as error:
            fb_log.error(f"Failed to save the floating browser config: {error}")
            show_info_bar(
                self.window(),
                self.tr("Failed to save the floating browser config: ") + str(error),
                title=self.tr("Save failed"),
                error=True,
            )
            show_info_bar(self.window(), str(error), title=self.tr("Error"), error=True)

    def _persist_window_geometry(self):
        """把用户在悬浮窗上拖动/缩放得到的尺寸写回配置文件。"""
        if not self.service.running:
            return
        try:
            from ok import og

            _, _, width, height = self.service.geometry()
            if width <= 0 or height <= 0:
                return
            browser_config = og.executor.global_config.get_config(FLOATING_BROWSER_CONFIG)
            browser_config["width"] = int(width)
            browser_config["height"] = int(height)
            browser_config["opacity"] = round(float(self.service.browser.opacity), 2)
            browser_config["click_through"] = bool(self.service.browser.click_through_enabled)
            if hasattr(browser_config, "save_file"):
                browser_config.save_file()
        except Exception as error:
            fb_log.debug(f"Failed to save the window geometry: {error}")

    def _reload_hotkeys(self):
        """Save first, then restart the floating browser to rebind the hotkeys."""
        self._save_config()
        if self.service.running:
            self.service.stop()
            self.service.start()
        else:
            show_info_bar(
                self.window(),
                self.tr("The floating browser is not running; hotkeys apply on start"),
                title=self.tr("Notice"),
            )

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _use_preset(self, url: str):
        self.url_edit.setText(url)

    def _on_hover_opacity_changed(self, value: int) -> None:
        """实时把「穿透悬停透明度」应用到运行中的悬浮窗。"""
        self.service.set_hover_opacity(value / 100.0)

    def _on_log_output_changed(self) -> None:
        """日志总开关：勾/取消即时生效（不需要重启悬浮浏览器）。"""
        self.service.set_verbose(self.log_output_check.isChecked())

    def _start(self):
        url = normalize_url(self.url_edit.text())
        webview_mode = self.service.start(url)
        if webview_mode:
            if self.service.failed_hotkeys:
                show_info_bar(
                    self.window(),
                    self.tr("These hotkeys are taken by other programs: ") + "、".join(self.service.failed_hotkeys)
                    + self.tr(", please pick different combinations in Global hotkeys."),
                    title=self.tr("Some hotkeys unavailable"),
                    error=True,
                )
            else:
                show_info_bar(self.window(), self.tr("Floating browser started"), title=self.tr("Success"))
        else:
            show_info_bar(
                self.window(),
                self.tr("WebView2 runtime not found; opening the video page in the system browser instead."
                        "Install WebView2 to get the floating window and hotkey control."),
                title=self.tr("Running in degraded mode"),
            )
        self._refresh_status()

    def _stop(self):
        self.service.stop()
        self._refresh_status()

    def _toggle_visible(self):
        self.service.toggle_visible()
        self._refresh_status()

    # ------------------------------------------------------------------
    # 状态刷新
    # ------------------------------------------------------------------
    def _poll_state(self):
        running = self.service.running
        if running != self._last_running:
            # 运行状态变化（例如用户点了悬浮窗上的关闭按钮），刷新按钮可用性
            self._last_running = running
            self._refresh_status()
        if not running:
            return
        for event in self.service.drain_ui_events():
            self._apply_ui_event(event)
        state = self.service.refresh_state()
        self.state_refreshed.emit(state)

    def _on_state_changed(self, state):
        # 来自后台线程，切换到 Qt 主线程更新界面
        self.state_refreshed.emit(state)

    def _on_ui_event(self, payload):
        if not isinstance(payload, dict):
            return
        self.state_refreshed.emit(None)
        if payload.get("close"):
            # 用户点了悬浮窗上的关闭按钮
            self.notice_requested.emit(self.tr("Floating browser closed"), self.tr("Notice"))
            return
        if "click_through" in payload:
            flag = self.tr("On") if payload["click_through"] else self.tr("Off")
            # 通过信号切回主线程再弹提示，禁止在后台线程直接操作 Qt UI
            self.notice_requested.emit(
                self.tr("Click-through") + flag + self.tr(", press the toggle click-through hotkey to turn it off."),
                self.tr("Notice"),
            )

    def _show_notice(self, message: str, title: str) -> None:
        show_info_bar(self.window(), message, title=title)

    def _apply_ui_event(self, payload):
        if "opacity" in payload:
            self._persist_window_geometry()

    def _render_state(self, state):
        if state is None:
            return
        if not state.found:
            self.status_title.setText(self.tr("Started, no video detected"))
            self.status_detail.setText(
                self.tr("Open a video page in the floating window and start playback; hotkeys work from then on.")
            )
            return
        flag = self.tr("Paused") if state.paused else self.tr("Playing")
        self.status_title.setText(
            f"{flag}  {_format_time(state.current)} / {_format_time(state.duration)}"
        )
        _, _, width, height = self.service.geometry()
        browser = self.service.browser
        opacity = browser.opacity if browser is not None else 0.9
        details = [
            self.tr("Playback speed") + f": {state.rate:.2f}x",
            self.tr("Volume") + f": {int(state.volume * 100)}%"
            + (self.tr(" (muted)") if state.muted else ""),
            self.tr("Window") + f": {width}x{height}",
            self.tr("Opacity") + f": {int(round(opacity * 100))}%",
        ]
        if state.title:
            details.append(self.tr("Title") + f": {state.title}")
        self.status_detail.setText("    ".join(details))

    def _refresh_status(self):
        running = self.service.running
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.hide_button.setEnabled(running)
        if not running:
            self.status_title.setText(self.tr("Not running"))
            self.status_detail.setText(self.service.last_message or "")
        else:
            self._poll_state()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_status()
