#!/usr/bin/env python3
"""System tray app for ROSESELSA earbuds (tested with ROSE Ceramics Ultra).

Left click opens the control panel, right click opens a quick menu.
Start with --show to open the panel right away.
"""
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QObject, QPointF, QRectF, QSize, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox,
    QProgressBar, QPushButton, QSizePolicy, QSystemTrayIcon, QToolButton, QVBoxLayout, QWidget,
)

import rosebuds as r

REFRESH_MS = 60_000
# Same order as the ROSELINK app.
MODE_LABELS = {"on": "ANC", "wind": "Wind", "off": "Off", "trans": "Transparency"}
LEVEL_LABELS = {"light": "Light", "moderate": "Moderate", "deep": "Deep"}
MODE_OPTIONS = [(r.MODES[k], label) for k, label in MODE_LABELS.items()]
LEVEL_OPTIONS = [(r.LEVELS[k], label) for k, label in LEVEL_LABELS.items()]
EQ_LABELS = {"classic": "Classic", "pop": "POP", "hifi": "HIFI", "rock": "ROCK", "custom": "Custom"}
EQ_OPTIONS = [(r.EQ_PRESETS[k], label) for k, label in EQ_LABELS.items()]
CODEC_LABELS = {"aac": "AAC/SBC", "ldac": "LDAC", "lhdc": "LHDC"}
CODEC_OPTIONS = [(r.CODECS[k], label) for k, label in CODEC_LABELS.items()]
DUAL_ONLY_AAC = "Turn off dual-device connection to use this codec."
NO_REPLY = "No reply. Is the phone app connected to the earbuds?"
DEFAULT_NAME = "ROSE earbuds"
UNTESTED = "Untested model. Settings may not match what you pick."
RESTARTING = "The earbuds are restarting…"
# The earbuds take a few seconds to restart and reconnect; check twice in case the first is too early.
RESTART_CHECKS_MS = (8_000, 20_000)
# Breeze's positive and negative colors.
BATTERY_OK = "#27ae60"
BATTERY_LOW = "#da4453"

ICON_DIR = Path(__file__).resolve().parent / "icons"
ICON_SIZES = (16, 22, 24, 32, 48, 64)
# The color the SVGs use for ColorScheme-Text; Plasma replaces it when it loads them.
ICON_BASE_COLOR = "#232629"


def battery_text(settings):
    parts = []
    for name, pct, charging in r.battery_levels(settings):
        value = "–" if pct is None else f"{pct}%" + (" ⚡" if charging else "")
        parts.append(f"{name.capitalize()} {value}")
    return " · ".join(parts) or "Battery unknown"


def render_svg(name, color, size, opacity=1.0):
    svg = (ICON_DIR / f"{name}.svg").read_text().replace(ICON_BASE_COLOR, color)
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setOpacity(opacity)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    return pixmap


def button_icon(name):
    """Load an icon for a segmented button, colored like the button text in each state."""
    pal = QApplication.palette()
    text = pal.color(QPalette.ColorRole.WindowText)
    icon = QIcon()
    for size in (18, 36):
        for mode in (QIcon.Mode.Normal, QIcon.Mode.Active):
            icon.addPixmap(render_svg(name, text.name(), size), mode, QIcon.State.Off)
            icon.addPixmap(render_svg(name, pal.color(QPalette.ColorRole.HighlightedText).name(), size),
                           mode, QIcon.State.On)
        for state in (QIcon.State.Off, QIcon.State.On):
            icon.addPixmap(render_svg(name, text.name(), size, 0.35), QIcon.Mode.Disabled, state)
    return icon


def load_icon(name):
    """Load an icon from icons/, preferring the installed copy.

    When installed into an icon theme, the tray passes only the name to Plasma,
    which colors the icon to match the panel. Otherwise Plasma can't find it by
    name, so render the file here in the color scheme's text color.
    """
    if QIcon.hasThemeIcon(name):
        return QIcon.fromTheme(name)
    color = QApplication.palette().color(QPalette.ColorRole.WindowText).name()
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(render_svg(name, color, size))
    return icon


def stylesheet():
    """Build the panel style from the current Qt palette (the desktop color scheme)."""
    pal = QApplication.palette()
    text = pal.color(QPalette.ColorRole.WindowText)

    def color(role):
        return pal.color(role).name()

    def tint(alpha):
        return f"rgba({text.red()}, {text.green()}, {text.blue()}, {alpha})"

    return f"""
    QWidget#panel {{ background: {color(QPalette.ColorRole.Window)}; }}
    QFrame#card {{ background: {color(QPalette.ColorRole.Base)}; border-radius: 14px; }}
    QLabel#title {{ font-size: 15pt; font-weight: 600; }}
    QLabel#cardTitle, QLabel#value {{ font-weight: 600; }}
    QLabel#muted, QLabel#state {{ color: {tint(0.6)}; }}
    QLabel#state[ok="true"] {{ color: {color(QPalette.ColorRole.Highlight)}; }}
    QProgressBar {{ background: {tint(0.12)}; border: none; border-radius: 3px; }}
    QProgressBar::chunk {{ background: {BATTERY_OK}; border-radius: 3px; }}
    QProgressBar[low="true"]::chunk {{ background: {BATTERY_LOW}; }}
    QProgressBar::chunk:disabled {{ background: {tint(0.3)}; }}
    QLabel#muted:disabled, QLabel#value:disabled {{ color: {tint(0.35)}; }}
    QFrame#segmented {{ background: {tint(0.07)}; border-radius: 10px; }}
    QFrame#segmented QAbstractButton {{
        border: none; border-radius: 8px; padding: 7px 4px;
        background: transparent; color: {color(QPalette.ColorRole.WindowText)};
    }}
    QFrame#segmented QAbstractButton:hover:!checked {{ background: {tint(0.08)}; }}
    QFrame#segmented QAbstractButton:checked {{
        background: {color(QPalette.ColorRole.Highlight)};
        color: {color(QPalette.ColorRole.HighlightedText)};
    }}
    QFrame#segmented QAbstractButton:disabled {{ color: {tint(0.35)}; }}
    QFrame#segmented QAbstractButton:checked:disabled {{ background: {tint(0.18)}; }}
    """


def repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class Switch(QAbstractButton):
    """An on/off toggle in the style of the phone app."""

    def __init__(self):
        super().__init__()
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(44, 24)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        if self.isChecked():
            track = QColor(pal.color(QPalette.ColorRole.Highlight))
        else:
            track = QColor(pal.color(QPalette.ColorRole.WindowText))
            track.setAlphaF(0.3)
        if not self.isEnabled():
            track.setAlphaF(track.alphaF() * 0.4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(self.rect(), 12, 12)
        p.setBrush(QColor("white"))
        x = self.width() - 12 if self.isChecked() else 12
        p.drawEllipse(QPointF(x, 12), 9, 9)


class Segmented(QFrame):
    """A row of exclusive buttons. Emits the picked value on user clicks only."""
    picked = Signal(int)

    def __init__(self, options, icons=None):
        super().__init__()
        self.icons = icons or {}
        self.setObjectName("segmented")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        self.group = QButtonGroup(self)
        for value, label in options:
            if value in self.icons:
                button = QToolButton()
                button.setText(label)
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
                button.setIconSize(QSize(18, 18))
            else:
                button = QPushButton(label)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.group.addButton(button, value)
            layout.addWidget(button)
        self.group.idClicked.connect(self.picked)
        self.update_icons()

    def update_icons(self):
        for value, name in self.icons.items():
            self.group.button(value).setIcon(button_icon(name))

    def set_value(self, value):
        button = self.group.button(value)
        if button:
            button.setChecked(True)
            return
        # Unknown value: show nothing selected.
        self.group.setExclusive(False)
        for b in self.group.buttons():
            b.setChecked(False)
        self.group.setExclusive(True)


class BatteryGauge(QWidget):
    def __init__(self, name):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        top = QHBoxLayout()
        label = QLabel(name)
        label.setObjectName("muted")
        self.value = QLabel("–")
        self.value.setObjectName("value")
        top.addWidget(label)
        top.addStretch()
        top.addWidget(self.value)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        layout.addLayout(top)
        layout.addWidget(self.bar)

    def set_level(self, pct, charging):
        self.bar.setValue(pct or 0)
        self.value.setText("–" if pct is None else f"{pct}%" + (" ⚡" if charging else ""))
        self.bar.setProperty("low", pct is not None and pct <= 20 and not charging)
        repolish(self.bar)


class Panel(QWidget):
    """The window shown on left click."""

    def __init__(self, tray):
        super().__init__()
        self.setObjectName("panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setWindowTitle(DEFAULT_NAME)
        self.setWindowIcon(load_icon("rosebuds"))
        self.setFixedWidth(460)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel(DEFAULT_NAME)
        self.title.setObjectName("title")
        self.state = QLabel("Connecting…")
        self.state.setObjectName("state")
        self.state.setWordWrap(True)
        titles.addWidget(self.title)
        titles.addWidget(self.state)
        header.addLayout(titles, 1)
        refresh = QToolButton()
        refresh.setIcon(QIcon.fromTheme("view-refresh"))
        refresh.setToolTip("Refresh")
        refresh.setAutoRaise(True)
        refresh.clicked.connect(tray.refresh)
        header.addWidget(refresh, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(header)

        card = self._card(root)
        row = QHBoxLayout()
        row.setSpacing(18)
        self.gauges = [BatteryGauge(name) for name in ("Left", "Right", "Case")]
        for gauge in self.gauges:
            row.addWidget(gauge)
        card.addLayout(row)

        card = self._card(root, "Noise control")
        self.modes = Segmented([(v, "Transp." if label == "Transparency" else label) for v, label in MODE_OPTIONS],
                               {r.MODES[k]: f"mode-{k}" for k in MODE_LABELS})
        self.modes.group.button(r.MODES["trans"]).setToolTip("Transparency")
        self.modes.picked.connect(lambda v: tray.run(lambda b: b.set_mode(v)))
        card.addWidget(self.modes)
        card.addSpacing(4)
        card.addWidget(self._label("ANC level", "muted"))
        self.levels = Segmented(LEVEL_OPTIONS)
        self.levels.picked.connect(lambda v: tray.run(lambda b: b.set_level(v)))
        card.addWidget(self.levels)
        # The level only matters in ANC mode, so follow the mode selection right away.
        self.modes.picked.connect(lambda v: self.levels.setEnabled(v == r.MODES["on"]))

        card = self._card(root, "Equalizer")
        self.eq = Segmented(EQ_OPTIONS)
        self.eq.picked.connect(lambda v: tray.run(lambda b: b.set_eq(v)))
        self.eq.group.button(r.EQ_PRESETS["custom"]).setToolTip("Last custom curve set in ROSELINK")
        card.addWidget(self.eq)

        card = self._card(root)
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(self._label("Game mode", "cardTitle"))
        text.addWidget(self._label("Lower latency, faster sound response", "muted"))
        row.addLayout(text, 1)
        self.game = Switch()
        self.game.clicked.connect(lambda on: tray.run(lambda b: b.set_game(on)))
        row.addWidget(self.game)
        card.addLayout(row)

        card = self._card(root)
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(self._label("Dual-device connection", "cardTitle"))
        note = self._label("Connect to two devices at once. Only AAC and SBC codecs work in this mode.", "muted")
        note.setWordWrap(True)
        text.addWidget(note)
        row.addLayout(text, 1)
        self.dual = Switch()
        self.dual.clicked.connect(lambda on: tray.switch_dual(on, self))
        row.addWidget(self.dual)
        card.addLayout(row)

        card = self._card(root, "Audio codec")
        self.codec = Segmented(CODEC_OPTIONS)
        self.codec.picked.connect(lambda v: tray.switch_codec(v, self))
        card.addWidget(self.codec)
        card.addWidget(self._label("Switching audio codec will restart the earbuds.", "muted"))

        self.controls = [*self.gauges, self.modes, self.levels, self.eq, self.game, self.dual, self.codec]
        self._styling = False
        self._restyle()
        # Size it now: on first show, Qt caps the window at 2/3 of the screen height, which squeezes the buttons.
        self._fit_height()

    @staticmethod
    def _label(text, name):
        label = QLabel(text)
        label.setObjectName(name)
        return label

    def _card(self, parent_layout, title=None):
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)
        if title:
            layout.addWidget(self._label(title, "cardTitle"))
        parent_layout.addWidget(frame)
        return layout

    def _fit_height(self):
        # Not adjustSize(): it caps visible windows at 2/3 of the screen height.
        self.layout().activate()
        self.setFixedHeight(self.sizeHint().height())

    def _restyle(self):
        self._styling = True
        self.setStyleSheet(stylesheet())
        self.modes.update_icons()
        self._styling = False

    def changeEvent(self, event):
        # Follow desktop color scheme switches while running.
        if event.type() == QEvent.Type.PaletteChange and not self._styling:
            self._restyle()
        super().changeEvent(event)

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def show_error(self, message):
        self.state.setText(message)
        self.state.setProperty("ok", False)
        repolish(self.state)
        for widget in self.controls:
            widget.setEnabled(False)
        self._fit_height()

    def show_settings(self, device, settings):
        self.title.setText(device.alias)
        self.setWindowTitle(device.alias)
        self.state.setText("Connected" if device.tested else f"Connected · {UNTESTED}")
        self.state.setProperty("ok", True)
        repolish(self.state)
        for gauge, (_, pct, charging) in zip(self.gauges, r.battery_levels(settings)):
            gauge.set_level(pct, charging)
        mode = settings.get(r.MODE_KEY, b"\xff")[0]
        self.modes.set_value(mode)
        self.levels.set_value(settings.get(r.LEVEL_KEY, b"\xff")[0])
        self.eq.set_value(settings.get(r.EQ_KEY, b"\xff")[0])
        self.game.setChecked(settings.get(r.GAME_KEY) == b"\x01")
        dual = settings.get(r.DUAL_KEY) == b"\x01"
        self.dual.setChecked(dual)
        self.codec.set_value(settings.get(r.CODEC_KEY, b"\xff")[0])
        for widget in self.controls:
            widget.setEnabled(True)
        for value, button in ((v, self.codec.group.button(v)) for v, _ in CODEC_OPTIONS):
            allowed = value == r.CODECS["aac"] or not dual
            button.setEnabled(allowed)
            button.setToolTip("" if allowed else DUAL_ONLY_AAC)
        self.levels.setEnabled(mode == r.MODES["on"])
        self._fit_height()


class Tray(QObject):
    # Emitted from the worker thread: (Device, settings dict), or an error message.
    result = Signal(object)

    def __init__(self):
        super().__init__()
        # One worker, so only one connection to the earbuds is open at a time.
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.pending = 0
        self.settings = {}
        self.result.connect(self._show)
        self.panel = Panel(self)

        self.menu = QMenu()
        # Info lines, not clickable.
        self.title = self.menu.addAction(DEFAULT_NAME)
        self.title.setEnabled(False)
        self.status = self.menu.addAction("Connecting…")
        self.status.setEnabled(False)

        self.menu.addSeparator()
        self.modes = self._radio_group(self.menu, MODE_OPTIONS, lambda v: self.run(lambda b: b.set_mode(v)))

        # Less used settings go in submenus to keep the menu short.
        self.menu.addSeparator()
        self.level_menu = self.menu.addMenu("ANC level")
        self.levels = self._radio_group(self.level_menu, LEVEL_OPTIONS,
                                        lambda v: self.run(lambda b: b.set_level(v)))
        self.eq_menu = self.menu.addMenu("Equalizer")
        self.eqs = self._radio_group(self.eq_menu, EQ_OPTIONS, lambda v: self.run(lambda b: b.set_eq(v)))
        self.game = self.menu.addAction("Game mode")
        self.game.setCheckable(True)
        self.game.triggered.connect(lambda on: self.run(lambda b: b.set_game(on)))

        self.menu.addSeparator()
        self.menu.addAction("Open panel…", self.show_panel)
        self.menu.addAction("Quit", QApplication.quit)
        self.menu.aboutToShow.connect(self.refresh)
        self.controls = [*self.modes.values(), self.level_menu.menuAction(),
                         self.eq_menu.menuAction(), self.game]

        self.connected = False
        self.icons = {}
        self.tray = QSystemTrayIcon()
        self._update_icons()
        QApplication.instance().installEventFilter(self)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)
        self.name = DEFAULT_NAME
        self.tray.setToolTip(self.name)
        self.tray.show()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)
        self.refresh()

    @staticmethod
    def _radio_group(menu, options, on_pick):
        group = QActionGroup(menu)
        actions = {}
        for value, label in options:
            action = QAction(label, menu, checkable=True)
            action.triggered.connect(partial(on_pick, value))
            group.addAction(action)
            menu.addAction(action)
            actions[value] = action
        return actions

    def _on_activated(self, reason):
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        if self.panel.isVisible():
            self.panel.hide()
        else:
            self.show_panel()

    def show_panel(self):
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()
        self.refresh()

    def refresh(self):
        # The timer, the panel and the menu can overlap; one queued status read is enough.
        if not self.pending:
            self.run(lambda b: b.get_all())

    def run(self, job):
        self.pending += 1
        self.pool.submit(self._work, job)

    def switch_dual(self, on, parent):
        self._switch_restart(r.DUAL_KEY, int(on), "Switch dual-device connection",
                             "The earbuds restart when dual-device connection is turned on or off. Continue?",
                             "While it's on, the earbuds can connect to two devices at once. "
                             "Only AAC and SBC codecs work in this mode.", parent)

    def switch_codec(self, value, parent):
        name = {v: k for k, v in r.CODECS.items()}[value]
        label = CODEC_LABELS[name]
        info = "" if name == "aac" else (f"{label} may cause severe stuttering. The earbuds prefer it, "
                                         "but the playing device must support it too.")
        self._switch_restart(r.CODEC_KEY, value, "Switch audio codec",
                             f"The earbuds restart to switch to {label}. Continue?", info, parent)

    def _switch_restart(self, key, value, title, text, info, parent):
        # Keep showing the current value until the earbuds report the new one.
        self.panel.dual.setChecked(self.settings.get(r.DUAL_KEY) == b"\x01")
        self.panel.codec.set_value(self.settings.get(r.CODEC_KEY, b"\xff")[0])
        if self.settings.get(key) == bytes([value]):
            return
        box = QMessageBox(QMessageBox.Icon.Question, title, text,
                          QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok, parent)
        box.setInformativeText(info)
        box.button(QMessageBox.StandardButton.Ok).setText("Restart and switch")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Ok:
            return

        def job(buds):
            return RESTARTING if buds.set_restart(key, value) else NO_REPLY

        self.run(job)
        for ms in RESTART_CHECKS_MS:
            QTimer.singleShot(ms, self.refresh)

    def _work(self, job):
        try:
            buds = r.Earbuds()
            try:
                result = job(buds)
                self.result.emit(result if isinstance(result, str) else (buds.device, result))
            finally:
                buds.close()
        except r.EarbudsError as e:
            self.result.emit(str(e))
        except OSError as e:
            self.result.emit(f"Connection lost ({e.strerror or e})")

    def _update_icons(self):
        self.icons = {True: load_icon("rosebuds"), False: load_icon("rosebuds-disconnected")}
        self.tray.setIcon(self.icons[self.connected])
        self.panel.setWindowIcon(self.icons[True])

    def eventFilter(self, obj, event):
        # Redraw the icons when the desktop color scheme changes.
        if obj is QApplication.instance() and event.type() == QEvent.Type.ApplicationPaletteChange:
            self._update_icons()
        return False

    def _set_connected(self, connected):
        self.connected = connected
        self.tray.setIcon(self.icons[connected])

    def _show(self, result):
        self.pending -= 1
        device, settings = (None, None) if isinstance(result, str) else result
        if not settings:
            message = result if isinstance(result, str) else NO_REPLY
            self.status.setText("Not available")
            self._set_connected(False)
            self.tray.setToolTip(f"{self.name}\n{message}")
            for action in self.controls:
                action.setEnabled(False)
            self.panel.show_error(message)
            return

        self.name = device.alias
        self.title.setText(self.name)
        text = battery_text(settings)
        self.status.setText(text)
        self._set_connected(True)
        self.tray.setToolTip(f"{self.name}\n{text}" + ("" if device.tested else f"\n{UNTESTED}"))
        for action in self.controls:
            action.setEnabled(True)
        for key, actions in ((r.MODE_KEY, self.modes), (r.LEVEL_KEY, self.levels), (r.EQ_KEY, self.eqs)):
            value = settings.get(key, b"\xff")[0]
            for v, action in actions.items():
                action.setChecked(v == value)
        for menu, title, key, options in ((self.level_menu, "ANC level", r.LEVEL_KEY, LEVEL_OPTIONS),
                                          (self.eq_menu, "Equalizer", r.EQ_KEY, EQ_OPTIONS)):
            current = dict(options).get(settings.get(key, b"\xff")[0])
            menu.setTitle(f"{title}: {current}" if current else title)
        self.game.setChecked(settings.get(r.GAME_KEY) == b"\x01")
        self.settings = settings
        self.panel.show_settings(device, settings)


def main():
    # Python can't run its Ctrl+C handler while Qt's event loop is waiting, so let the signal end the process.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)
    app.setApplicationName(DEFAULT_NAME)
    app.setDesktopFileName("rosebuds")
    app.setQuitOnLastWindowClosed(False)

    # Only one copy runs. Starting another opens the running one's panel.
    runtime_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation)
    socket_path = str(Path(runtime_dir) / "rosebuds-tray")
    running = QLocalSocket()
    running.connectToServer(socket_path)
    if running.waitForConnected(1000):
        return
    # A copy that crashed or was stopped with Ctrl+C leaves its socket file behind.
    QLocalServer.removeServer(socket_path)
    server = QLocalServer()
    if not server.listen(socket_path):
        sys.exit(f"Can't listen on {socket_path}: {server.errorString()}")

    if not QSystemTrayIcon.isSystemTrayAvailable():
        sys.exit("No system tray available.")
    tray = Tray()
    if "--show" in sys.argv[1:]:
        tray.show_panel()

    def on_launch():
        server.nextPendingConnection().deleteLater()
        tray.show_panel()

    server.newConnection.connect(on_launch)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
