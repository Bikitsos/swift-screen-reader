"""System tray app: keeps the capture service resident so screenshots are taken instantly when called."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, CLI_EXE_NAME, COMPANY, CREDITS, EXE_NAME, __version__, build_info, config, ipc, pipe_name
from .config import DEFAULT_FILE_NAME, DEFAULT_HOTKEY_CAPTURE, DEFAULT_HOTKEY_SELECT, DEFAULT_PROCESSING_TEXT, Area
from .hotkeys import HotkeyManager, display
from .overlay import ProcessingOverlay
from .selector import AreaHighlight, AreaSelector
from .service import CaptureService
from .updater import Release, UpdateChecker, UpdateDownloader, can_self_update, is_newer, run_installer

COORD_LIMIT = 100_000
CAPTURE, SELECT = "Capture now", "Select area"
PORTABLE = QKeySequence.SequenceFormat.PortableText


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setStyleSheet("color: #777;")
    return label


def make_icon(active: bool) -> QIcon:
    """Screenshot-style icon: four corner brackets around a filled area."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor("#1e88e5") if active else QColor("#9e9e9e")
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color.lighter(150))
    p.drawRoundedRect(14, 14, 36, 36, 4, 4)
    pen = QPen(color, 7)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    for x, y, dx, dy in ((6, 6, 1, 1), (58, 6, -1, 1), (6, 58, 1, -1), (58, 58, -1, -1)):
        p.drawLine(x, y, x + 14 * dx, y)
        p.drawLine(x, y, x, y + 14 * dy)
    p.end()
    return QIcon(pixmap)


class LogEmitter(QObject):
    message = Signal(str)


class QtLogHandler(logging.Handler):
    def __init__(self, emitter: LogEmitter) -> None:
        super().__init__()
        self.emitter = emitter

    def emit(self, record: logging.LogRecord) -> None:
        self.emitter.message.emit(self.format(record))


class MainWindow(QMainWindow):
    def __init__(self, log_emitter: LogEmitter) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(760, 690)
        self.settings = config.load()
        self.service = CaptureService(self.settings, self)
        self.hotkeys = HotkeyManager(self)
        self.hotkeys.triggered.connect(self._on_hotkey)
        self.last_path: str | None = None
        self._selector: AreaSelector | None = None
        self._service_started = False
        self._loading = False
        self._tray_hint_shown = False
        self._tray_click = self.open_last
        self._update_silent = False
        self._icon_on, self._icon_off = make_icon(True), make_icon(False)
        self.setWindowIcon(self._icon_on)
        self.update_checker = UpdateChecker(self)
        self.update_checker.finished.connect(self._on_update_checked)
        self._save_timer = QTimer(self, singleShot=True, interval=400)
        self._save_timer.timeout.connect(self._save_settings)
        self._http_timer = QTimer(self, singleShot=True, interval=800)
        self._http_timer.timeout.connect(self._restart_http)

        self._build_ui()
        self._build_tray()
        self._load_settings()
        log_emitter.message.connect(self.log_view.appendPlainText)
        logging.info("%s %s (%s)", APP_NAME, __version__, build_info()[0])

        self.service.captured.connect(self._on_captured)
        self.service.grabbed.connect(self._on_grabbed)
        self.processing = ProcessingOverlay(self)
        self.service.failed.connect(self._on_failed)
        self.service.command.connect(self._on_command)
        QApplication.instance().focusChanged.connect(self._on_focus_changed)

    # -- UI -------------------------------------------------------------------

    def _build_ui(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction("Capture now", self.capture_now, "Ctrl+Shift+S")
        file_menu.addAction("Select area...", self.select_area)
        file_menu.addAction("Open screenshots folder", self.open_folder)
        file_menu.addSeparator()
        file_menu.addAction("Quit", self.quit)
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction("Check for updates...", lambda: self.check_for_updates(silent=False))
        help_menu.addAction("About", self.show_about)

        central = QWidget()
        layout = QVBoxLayout(central)

        area_box = QGroupBox("Capture area (physical pixels)")
        area_layout = QVBoxLayout(area_box)
        self.area_label = QLabel()
        self.area_label.setStyleSheet("font-size: 11pt; font-weight: bold;")
        area_layout.addWidget(self.area_label)
        coords = QHBoxLayout()
        self.x_spin, self.y_spin, self.w_spin, self.h_spin = (QSpinBox() for _ in range(4))
        for label, spin, minimum in (("X", self.x_spin, -COORD_LIMIT), ("Y", self.y_spin, -COORD_LIMIT),
                                     ("Width", self.w_spin, 0), ("Height", self.h_spin, 0)):
            spin.setRange(minimum, COORD_LIMIT)
            spin.valueChanged.connect(self._on_area_edited)
            coords.addWidget(QLabel(label))
            coords.addWidget(spin, 1)
        area_layout.addLayout(coords)
        buttons = QHBoxLayout()
        select = QPushButton("Select area...")
        select.clicked.connect(self.select_area)
        show = QPushButton("Show area")
        show.clicked.connect(self.show_area)
        capture = QPushButton("Capture now")
        capture.clicked.connect(self.capture_now)
        self.open_last_button = QPushButton("Open last screenshot")
        self.open_last_button.clicked.connect(self.open_last)
        self.open_last_button.setEnabled(False)
        for button in (select, show, capture, self.open_last_button):
            buttons.addWidget(button)
        area_layout.addLayout(buttons)
        layout.addWidget(area_box)

        output_box = QGroupBox("Output")
        form = QFormLayout(output_box)
        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText(str(config.Settings().screenshots_dir()))
        self.folder_edit.textChanged.connect(self._schedule_save)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_folder)
        open_folder = QPushButton("Open")
        open_folder.clicked.connect(self.open_folder)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        folder_row.addWidget(open_folder)
        form.addRow("Folder:", folder_row)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(DEFAULT_FILE_NAME)
        self.name_edit.textChanged.connect(self._schedule_save)
        form.addRow("File name:", self.name_edit)
        form.addRow("", _hint("Placeholders: {timestamp}, {date}, {time}.\n"
                              "A fixed name such as latest.png is overwritten on every capture."))
        self.clipboard_check = QCheckBox("Also copy the screenshot to the clipboard")
        self.clipboard_check.toggled.connect(self._schedule_save)
        self.notify_check = QCheckBox("Show a notification after each capture")
        self.notify_check.toggled.connect(self._schedule_save)
        form.addRow("", self.clipboard_check)
        form.addRow("", self.notify_check)
        cover_row = QHBoxLayout()
        self.processing_check = QCheckBox("After each capture, cover the screen for")
        self.processing_check.toggled.connect(self._schedule_save)
        self.processing_spin = QSpinBox()
        self.processing_spin.setRange(1, 60)
        self.processing_spin.setSuffix(" s")
        self.processing_spin.valueChanged.connect(self._schedule_save)
        self.processing_edit = QLineEdit()
        self.processing_edit.setPlaceholderText(DEFAULT_PROCESSING_TEXT)
        self.processing_edit.textChanged.connect(self._schedule_save)
        cover_row.addWidget(self.processing_check)
        cover_row.addWidget(self.processing_spin)
        cover_row.addWidget(QLabel("showing"))
        cover_row.addWidget(self.processing_edit, 1)
        form.addRow("", cover_row)
        layout.addWidget(output_box)

        keys_box = QGroupBox("Keyboard shortcuts (work anywhere, also while the app is in the tray)")
        keys_form = QFormLayout(keys_box)
        self.hotkey_edits: dict[str, QKeySequenceEdit] = {}
        for name, default in ((CAPTURE, DEFAULT_HOTKEY_CAPTURE), (SELECT, DEFAULT_HOTKEY_SELECT)):
            edit = QKeySequenceEdit()
            edit.setMaximumSequenceLength(1)
            edit.setClearButtonEnabled(True)
            edit.editingFinished.connect(self._schedule_save)
            edit.keySequenceChanged.connect(self._schedule_save)
            reset = QPushButton("Default")
            reset.clicked.connect(lambda _=False, e=edit, d=default: self._reset_hotkey(e, d))
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            row.addWidget(reset)
            keys_form.addRow(f"{name}:", row)
            self.hotkey_edits[name] = edit
        self.hotkey_status = QLabel()
        keys_form.addRow("", self.hotkey_status)
        layout.addWidget(keys_box)
        service_box = QGroupBox("Service")
        service_form = QFormLayout(service_box)
        self.status_label = QLabel()
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        service_form.addRow("Status:", self.status_label)
        http_row = QHBoxLayout()
        self.http_check = QCheckBox("Local HTTP endpoint on 127.0.0.1, port")
        self.http_check.toggled.connect(self._on_http_changed)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.valueChanged.connect(self._on_http_changed)
        http_row.addWidget(self.http_check)
        http_row.addWidget(self.port_spin)
        http_row.addStretch(1)
        service_form.addRow("", http_row)
        self.update_check = QCheckBox("Check for updates at startup")
        self.update_check.toggled.connect(self._schedule_save)
        service_form.addRow("", self.update_check)
        service_form.addRow("", _hint(
            f"Call it with the shortcuts above, {CLI_EXE_NAME}.exe (prints the saved path),\n"
            f"{EXE_NAME}.exe --capture, or GET http://127.0.0.1:<port>/capture or /capture.png."
        ))
        layout.addWidget(service_box)

        layout.addWidget(QLabel("Activity"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        self.log_view.setMinimumHeight(60)
        layout.addWidget(self.log_view, 1)
        credits = QLabel(CREDITS)
        credits.setStyleSheet("color: #777;")
        layout.addWidget(credits)
        # Scroll instead of squashing the controls on small or highly scaled screens.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(central)
        self.setCentralWidget(scroll)

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self._icon_off, self)
        menu = QMenu(self)
        bold = QAction("Capture now", self)
        font = bold.font()
        font.setBold(True)
        bold.setFont(font)
        bold.triggered.connect(self.capture_now)
        menu.addAction(bold)
        select = menu.addAction("Select area...", self.select_area)
        self._tray_actions = {CAPTURE: (bold, "Capture now"), SELECT: (select, "Select area...")}
        menu.addAction("Show area", self.show_area)
        menu.addAction("Open screenshots folder", self.open_folder)
        menu.addSeparator()
        menu.addAction("Show window", self.show_window)
        menu.addAction("About", self.show_about)
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.messageClicked.connect(lambda: self._tray_click())
        self.tray.setToolTip(APP_NAME)
        self.tray.show()

    # -- settings -------------------------------------------------------------

    def _load_settings(self) -> None:
        self._loading = True
        s = self.settings
        area = s.area or Area(0, 0, 0, 0)
        self.x_spin.setValue(area.x)
        self.y_spin.setValue(area.y)
        self.w_spin.setValue(area.width)
        self.h_spin.setValue(area.height)
        self.folder_edit.setText(s.output_dir)
        self.name_edit.setText("" if s.file_name == DEFAULT_FILE_NAME else s.file_name)
        self.clipboard_check.setChecked(s.copy_to_clipboard)
        self.notify_check.setChecked(s.notify)
        self.http_check.setChecked(s.http_enabled)
        self.port_spin.setValue(s.http_port)
        self.update_check.setChecked(s.check_updates)
        self.hotkey_edits[CAPTURE].setKeySequence(QKeySequence.fromString(s.hotkey_capture, PORTABLE))
        self.hotkey_edits[SELECT].setKeySequence(QKeySequence.fromString(s.hotkey_select, PORTABLE))
        self.processing_check.setChecked(s.processing_enabled)
        self.processing_spin.setValue(s.processing_seconds)
        self.processing_edit.setText("" if s.processing_text == DEFAULT_PROCESSING_TEXT else s.processing_text)
        self._loading = False
        self._update_area_label()

    def _schedule_save(self) -> None:
        if self._loading:
            return
        s = self.settings
        s.output_dir = self.folder_edit.text().strip()
        s.file_name = self.name_edit.text().strip() or DEFAULT_FILE_NAME
        s.copy_to_clipboard = self.clipboard_check.isChecked()
        s.notify = self.notify_check.isChecked()
        s.http_enabled = self.http_check.isChecked()
        s.http_port = self.port_spin.value()
        s.check_updates = self.update_check.isChecked()
        s.hotkey_capture = self.hotkey_edits[CAPTURE].keySequence().toString(PORTABLE)
        s.hotkey_select = self.hotkey_edits[SELECT].keySequence().toString(PORTABLE)
        s.processing_enabled = self.processing_check.isChecked()
        s.processing_seconds = self.processing_spin.value()
        s.processing_text = self.processing_edit.text().strip() or DEFAULT_PROCESSING_TEXT
        self._save_timer.start()

    def _save_settings(self) -> None:
        try:
            config.save(self.settings)
        except OSError as exc:
            logging.error("Could not save settings: %s", exc)

    def _on_area_edited(self) -> None:
        if self._loading:
            return
        area = Area(self.x_spin.value(), self.y_spin.value(), self.w_spin.value(), self.h_spin.value())
        self.settings.area = area if area.valid else None
        self._update_area_label()
        self._schedule_save()

    def _set_area(self, area: Area) -> None:
        self._loading = True
        self.x_spin.setValue(area.x)
        self.y_spin.setValue(area.y)
        self.w_spin.setValue(area.width)
        self.h_spin.setValue(area.height)
        self._loading = False
        self.settings.area = area
        self._update_area_label()
        self._schedule_save()

    def _update_area_label(self) -> None:
        area = self.settings.area
        if area:
            self.area_label.setText(f"{area.width} x {area.height} pixels at ({area.x}, {area.y})")
        else:
            self.area_label.setText("No area selected yet. Click Select area... and drag over the screen.")

    def _on_http_changed(self) -> None:
        if self._loading:
            return
        self._schedule_save()
        self._http_timer.start()

    def _restart_http(self) -> None:
        self.service.restart_http()
        self._update_status()

    # -- keyboard shortcuts ---------------------------------------------------

    def _is_hotkey_widget(self, widget: QWidget | None) -> bool:
        return widget is not None and any(e is widget or e.isAncestorOf(widget) for e in self.hotkey_edits.values())

    def _on_focus_changed(self, old: QWidget | None, now: QWidget | None) -> None:
        was, is_ = self._is_hotkey_widget(old), self._is_hotkey_widget(now)
        if is_ and not was:
            # Pause the global shortcuts while recording, or pressing the current one would trigger it.
            self.hotkeys.stop()
        elif was and not is_:
            self.apply_hotkeys()

    def _reset_hotkey(self, edit: QKeySequenceEdit, default: str) -> None:
        edit.setKeySequence(QKeySequence.fromString(default, PORTABLE))
        self.apply_hotkeys()

    def apply_hotkeys(self) -> None:
        if not self._service_started:
            return
        shortcuts = {CAPTURE: self.settings.hotkey_capture, SELECT: self.settings.hotkey_select}
        errors = self.hotkeys.apply(shortcuts)
        active = [f"{name}: {display(text)}" for name, text in shortcuts.items() if text and name not in errors]
        if errors:
            self.hotkey_status.setStyleSheet("color: #c62828;")
            self.hotkey_status.setText("\n".join(errors.values()))
        elif active:
            self.hotkey_status.setStyleSheet("color: #2e7d32;")
            self.hotkey_status.setText("Active. Click a box and press new keys to change; clear it to turn it off.")
        else:
            self.hotkey_status.setStyleSheet("color: #777;")
            self.hotkey_status.setText("No shortcuts set.")
        for name, (action, label) in self._tray_actions.items():
            text = shortcuts[name]
            action.setText(f"{label}\t{display(text)}" if text and name not in errors else label)

    def _on_hotkey(self, name: str) -> None:
        if self._selector is not None:
            return
        if name == CAPTURE:
            self.capture_now()
        elif name == SELECT:
            self.select_area()

    def _update_status(self) -> None:
        parts = [f"Running. Pipe \\\\.\\pipe\\{pipe_name()}"]
        if self.service.http_running:
            parts.append(f"HTTP http://127.0.0.1:{self.settings.http_port}/")
        elif self.service.http_error:
            parts.append(f"HTTP off: {self.service.http_error}")
        self.status_label.setText("\n".join(parts))
        self.tray.setIcon(self._icon_on)
        self.tray.setToolTip(f"{APP_NAME} {__version__}: ready")

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Screenshots folder", str(self.settings.screenshots_dir()))
        if folder:
            self.folder_edit.setText(folder)

    # -- service --------------------------------------------------------------

    def start_service(self) -> bool:
        if not self.service.start():
            return False
        self._service_started = True
        self._update_status()
        self.apply_hotkeys()
        return True

    def _on_command(self, cmd: str) -> None:
        if cmd == "show":
            self.show_window()
        elif cmd == "select":
            self.select_area()
        elif cmd == "quit":
            QTimer.singleShot(0, self.quit)

    def _on_captured(self, path: str) -> None:
        self.last_path = path
        self.open_last_button.setEnabled(True)
        logging.info("Saved %s", path)
        if self.settings.copy_to_clipboard:
            image = QImage(path)
            if not image.isNull():
                QApplication.clipboard().setImage(image)
        if self.settings.notify:
            self._tray_click = self.open_last
            self.tray.showMessage(APP_NAME, f"Saved {Path(path).name}", QSystemTrayIcon.MessageIcon.Information, 2000)

    def _on_grabbed(self) -> None:
        s = self.settings
        if s.processing_enabled:
            self.processing.show(s.processing_text or DEFAULT_PROCESSING_TEXT, s.processing_seconds)

    def _on_failed(self, error: str) -> None:
        logging.error("Capture failed: %s", error)
        if self.settings.notify:
            self.tray.showMessage(APP_NAME, f"Capture failed: {error}", QSystemTrayIcon.MessageIcon.Warning, 4000)

    def capture_now(self) -> None:
        if self.settings.area is None:
            self.select_area()
            return
        try:
            self.service.capture()
        except Exception:  # noqa: BLE001 - already logged through the failed signal
            pass

    def select_area(self) -> None:
        if self._selector is not None:
            return
        was_visible = self.isVisible()
        self.hide()

        def done(area: Area | None) -> None:
            self._selector = None
            if area is not None:
                self._set_area(area)
                logging.info("Capture area set to %s", area)
                self.show_area()
            if was_visible or area is None and self.settings.area is None:
                self.show_window()

        # Give the window time to disappear so it isn't in the way of the selection.
        def start() -> None:
            self._selector = AreaSelector(self.settings.area, self)
            self._selector.finished.connect(done)
            self._selector.start()

        QTimer.singleShot(200, start)

    def show_area(self) -> None:
        if self.settings.area is None:
            QMessageBox.information(self, APP_NAME, "No capture area selected yet.")
            return
        highlight = AreaHighlight(self.settings.area)
        if highlight.valid:
            highlight.show()
        else:
            highlight.deleteLater()
            logging.warning("The capture area %s is not on any connected screen", self.settings.area)

    def open_folder(self) -> None:
        folder = self.settings.screenshots_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def open_last(self) -> None:
        if self.last_path and Path(self.last_path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_path))

    # -- updates --------------------------------------------------------------

    def check_for_updates(self, silent: bool = False) -> None:
        self._update_silent = silent
        logging.info("Checking for updates...")
        self.update_checker.check()

    def _on_update_checked(self, release: Release | None, error: str) -> None:
        silent = self._update_silent
        if error or release is None:
            logging.warning("Update check failed: %s", error)
            if not silent:
                QMessageBox.warning(self, "Check for updates", f"Could not check for updates.\n\n{error}")
            return
        if not is_newer(release.version):
            logging.info("Up to date (latest release is %s)", release.version)
            if not silent:
                QMessageBox.information(self, "Check for updates", f"You have the latest version ({__version__}).")
            return
        logging.info("Update available: %s", release.version)
        if silent:
            self._tray_click = lambda: self._prompt_update(release)
            self.tray.showMessage(
                "Update available",
                f"Version {release.version} is available. Click to update.",
                QSystemTrayIcon.MessageIcon.Information,
                10000,
            )
        else:
            self._prompt_update(release)

    def _prompt_update(self, release: Release) -> None:
        self.show_window()
        notes = release.notes.strip()
        if len(notes) > 1500:
            notes = notes[:1500] + "\n..."
        box = QMessageBox(self)
        box.setWindowTitle("Update available")
        box.setIconPixmap(self._icon_on.pixmap(64, 64))
        box.setText(f"Version {release.version} is available (you have {__version__}).")
        if notes:
            box.setDetailedText(notes)
        if can_self_update():
            box.setInformativeText("Download and install it now? The app will close and restart after the update.")
            install = box.addButton("Download and install", QMessageBox.ButtonRole.AcceptRole)
        else:
            box.setInformativeText("Automatic updates only work in the installed app. Open the release page instead?")
            install = None
        page = box.addButton("Open release page", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if install is not None and box.clickedButton() is install:
            self._download_update(release)
        elif box.clickedButton() is page:
            QDesktopServices.openUrl(QUrl(release.page_url))

    def _download_update(self, release: Release) -> None:
        dialog = QProgressDialog(f"Downloading {release.asset_name}...", "Cancel", 0, 100, self)
        dialog.setWindowTitle("Updating")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        downloader = UpdateDownloader(self)

        def on_progress(done: int, total: int) -> None:
            if total > 0:
                dialog.setValue(int(done * 100 / total))

        def on_finished(path: str, error: str) -> None:
            dialog.close()
            downloader.deleteLater()
            if error:
                if error != "Operation canceled":
                    logging.error("Update download failed: %s", error)
                    QMessageBox.warning(self, "Update failed", f"Could not download the update.\n\n{error}")
                return
            logging.info("Starting installer %s", path)
            try:
                run_installer(path)
            except OSError as exc:
                QMessageBox.warning(self, "Update failed", f"Could not start the installer.\n\n{exc}")
                return
            self.quit()

        downloader.progress.connect(on_progress)
        downloader.finished.connect(on_finished)
        dialog.canceled.connect(downloader.cancel)
        dialog.show()
        downloader.start(release)

    def show_about(self) -> None:
        build_date, commit = build_info()
        build = f"{build_date} ({commit})" if commit else build_date
        box = QMessageBox(self)
        box.setWindowTitle(f"About {APP_NAME}")
        box.setIconPixmap(self._icon_on.pixmap(64, 64))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"<h3>{APP_NAME}</h3>"
            f"<p>Version {__version__}<br>Build: {build}</p>"
            "<p>Stays resident in the system tray and saves a screenshot of a preselected "
            "screen area whenever it is called.</p>"
            f"<p><b>{CREDITS}</b></p>"
        )
        box.exec()

    # -- window / tray behaviour ----------------------------------------------

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def closeEvent(self, event: QCloseEvent) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(APP_NAME, "Still running in the system tray.", QSystemTrayIcon.MessageIcon.Information, 3000)
        else:
            self.quit()

    def quit(self) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save_settings()
        self.service.stop()
        self.hotkeys.stop()
        self.processing.hide()
        QApplication.instance().focusChanged.disconnect(self._on_focus_changed)
        self.tray.hide()
        QApplication.quit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=EXE_NAME, description=f"{APP_NAME} (system tray)")
    parser.add_argument("--minimized", action="store_true", help="start hidden in the system tray")
    parser.add_argument("--select", action="store_true", help="start by selecting the capture area")
    parser.add_argument("--capture", action="store_true", help="take a screenshot and exit (see the CLI for options)")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args, qt_args = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    # Single instance: hand the request to the running copy instead of starting another one.
    reply = ipc.request({"cmd": "select" if args.select else ("ping" if args.minimized else "show")})
    if reply is not None:
        return 0

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(COMPANY)
    app.setQuitOnLastWindowClosed(False)
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    emitter = LogEmitter()
    handler = QtLogHandler(emitter)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    window = MainWindow(emitter)
    if not window.start_service():
        QMessageBox.critical(None, APP_NAME, f"{APP_NAME} is already running or its pipe is in use.")
        return 1
    if args.select or window.settings.area is None and not args.minimized:
        window.show_window()
        if args.select:
            window.select_area()
    elif not args.minimized or not QSystemTrayIcon.isSystemTrayAvailable():
        window.show_window()
    if window.settings.check_updates:
        QTimer.singleShot(5000, lambda: window.check_for_updates(silent=True))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
