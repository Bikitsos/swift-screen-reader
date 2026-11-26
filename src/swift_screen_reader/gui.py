"""System tray app: keeps the capture service resident; each capture reads the amount and takes the payment."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import asdict
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, CLI_EXE_NAME, COMPANY, CREDITS, EXE_NAME, __version__, build_info, config, default_logs_dir, ipc, pipe_name
from .capture import CaptureError, capture_png
from .config import DEFAULT_AMOUNT_LABEL, DEFAULT_FILE_NAME, DEFAULT_HOTKEY_CAPTURE, DEFAULT_HOTKEY_SELECT, DEFAULT_HOTKEY_TAP, Area
from .hotkeys import HotkeyManager, display
from .ocr import OcrError, recognize
from .ocr import warm_up as ocr_warm_up
from .overlay import FAILURE, SUCCESS, WARNING, PaymentScreen
from .payment import APPROVED, DECLINED, ERROR, TERMINALS
from .selector import AreaHighlight, AreaSelector, PointHighlight, PointPicker
from .service import BusyError, CaptureService
from .tap import TAP_METHODS, TapError, parse_point, tap
from .elevation import is_elevated, relaunch_elevated, set_startup_task, startup_task_exists, window_owner_at
from .transaction import NO_AMOUNT, PAYING, READ, ZERO, Transaction, TransactionRunner, format_amount
from .updater import Release, UpdateChecker, UpdateDownloader, can_self_update, is_newer, run_installer

COORD_LIMIT = 100_000
CAPTURE, SELECT, PICK = "Capture now", "Select area", "Pick POS button"
# Wait after the payment screen closes before tapping, so the tap reaches the POS and not the closing cover.
TAP_DELAY_MS = 200
PORTABLE = QKeySequence.SequenceFormat.PortableText
LOG_NAME = "activity.log"

STATUS_COLORS = {
    APPROVED: "#2e7d32", DECLINED: "#c62828", ERROR: "#c62828", NO_AMOUNT: "#ef6c00", ZERO: "#ef6c00", READ: "#1565c0",
}
STATUS_TEXT = {
    APPROVED: "Approved", DECLINED: "Declined", ERROR: "Failed", NO_AMOUNT: "Amount not read", ZERO: "Nothing to pay",
    READ: "Amount read (payment off)", PAYING: "Processing payment...",
}

STYLE = """
#nav { border: none; background: palette(alternate-base); font-size: 11pt; padding: 6px; outline: 0; }
#nav::item { padding: 9px 14px; border-radius: 4px; }
#nav::item:selected { background: palette(highlight); color: palette(highlighted-text); }
#pageTitle { font-size: 16pt; font-weight: bold; }
#statusTitle { font-size: 18pt; font-weight: bold; }
#amount { font-size: 20pt; font-weight: bold; }
QPushButton#primary { background: #1e88e5; color: white; font-weight: bold; font-size: 11pt;
                      padding: 10px 26px; border: none; border-radius: 4px; }
QPushButton#primary:hover { background: #1976d2; }
QPushButton#primary:pressed { background: #1565c0; }
QPushButton#big { font-size: 11pt; padding: 9px 18px; }
"""


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setStyleSheet("color: #888;")
    return label


def _row(*widgets, stretch: bool = True) -> QHBoxLayout:
    row = QHBoxLayout()
    for widget in widgets:
        row.addWidget(widget)
    if stretch:
        row.addStretch(1)
    return row


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


def setup_file_logging() -> Path | None:
    """Detailed activity log in <app>/logs/activity.log, a new file each day, kept for 90 days."""
    folder = default_logs_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handler = TimedRotatingFileHandler(folder / LOG_NAME, when="midnight", backupCount=90, encoding="utf-8")
    except OSError as exc:
        logging.warning("Could not open the log file in %s: %s", folder, exc)
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.getLogger().addHandler(handler)
    return folder / LOG_NAME


class MainWindow(QMainWindow):
    def __init__(self, log_emitter: LogEmitter, log_file: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}" + (" (Administrator)" if is_elevated() else ""))
        self.resize(900, 640)
        self.log_file = log_file
        self.settings = config.load()
        self._saved = asdict(self.settings)
        self.service = CaptureService(self.settings, self)
        self.hotkeys = HotkeyManager(self)
        self.hotkeys.triggered.connect(self._on_hotkey)
        self.runner = TransactionRunner(self)
        self.runner.changed.connect(self._on_tx_changed)
        self.runner.finished.connect(self._on_tx_finished)
        self.screen_cover = PaymentScreen(self)
        self.screen_cover.dismissed.connect(self._on_result_dismissed)
        self.current_tx: Transaction | None = None
        self._tap_problem = ""
        self.last_tx: Transaction | None = None
        self.last_path: str | None = None
        self._selector: AreaSelector | None = None
        self._picker: PointPicker | None = None
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

        self.setStyleSheet(STYLE)
        self._build_ui()
        self._build_tray()
        self._load_settings()
        log_emitter.message.connect(self.log_view.appendPlainText)
        logging.info("%s %s (%s) started", APP_NAME, __version__, build_info()[0])
        if self.log_file:
            logging.info("Activity log: %s", self.log_file)

        self.service.captured.connect(self._on_captured)
        self.service.failed.connect(self._on_failed)
        self.service.command.connect(self._on_command)
        QApplication.instance().focusChanged.connect(self._on_focus_changed)

    # -- UI -------------------------------------------------------------------

    def _build_ui(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction("Capture now", lambda: self.capture_now("menu"), "Ctrl+Shift+S")
        file_menu.addAction("Select area...", self.select_area)
        file_menu.addAction("Open screenshots folder", self.open_folder)
        file_menu.addAction("Open activity log", self.open_log)
        file_menu.addSeparator()
        file_menu.addAction("Quit", self.quit)
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction("Check for updates...", lambda: self.check_for_updates(silent=False))
        help_menu.addAction("About", self.show_about)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(180)
        self.pages = QStackedWidget()
        for name, page in (
            ("Home", self._build_home_page()),
            ("Capture", self._build_capture_page()),
            ("Payment", self._build_payment_page()),
            ("Shortcuts", self._build_shortcuts_page()),
            ("Service", self._build_service_page()),
            ("Activity log", self._build_log_page()),
        ):
            self.nav.addItem(name)
            self.pages.addWidget(page)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.nav)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage(CREDITS)

    def _page(self, title: str, subtitle: str = "") -> tuple[QScrollArea, QVBoxLayout]:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 18, 24, 18)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        if subtitle:
            layout.addWidget(_hint(subtitle))
        layout.addSpacing(6)
        # Scroll instead of squashing the controls on small or highly scaled screens.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        return scroll, layout

    def _build_home_page(self) -> QWidget:
        page, layout = self._page("Home")
        self.status_title = QLabel()
        self.status_title.setObjectName("statusTitle")
        self.status_detail = QLabel()
        self.status_detail.setStyleSheet("font-size: 11pt;")
        layout.addWidget(self.status_title)
        layout.addWidget(self.status_detail)
        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet("color: #c62828; font-size: 11pt;")
        self.problem_button = QPushButton("Restart as administrator")
        self.problem_button.clicked.connect(self.restart_as_admin)
        layout.addWidget(self.problem_label)
        layout.addLayout(_row(self.problem_button))
        layout.addSpacing(10)

        capture = QPushButton("Capture now")
        capture.setObjectName("primary")
        capture.clicked.connect(lambda: self.capture_now("button"))
        select = QPushButton("Select area...")
        select.setObjectName("big")
        select.clicked.connect(self.select_area)
        show = QPushButton("Show area")
        show.setObjectName("big")
        show.clicked.connect(self.show_area)
        layout.addLayout(_row(capture, select, show))
        layout.addSpacing(14)

        box = QGroupBox("Last transaction")
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)
        self.tx_amount = QLabel("-")
        self.tx_amount.setObjectName("amount")
        self.tx_status = QLabel("No transaction yet")
        self.tx_status.setStyleSheet("font-size: 12pt; font-weight: bold;")
        self.tx_time = QLabel("-")
        self.tx_source = QLabel("-")
        self.tx_read = QLabel("-")
        self.tx_message = QLabel("-")
        self.tx_auth = QLabel("-")
        for label in (self.tx_read, self.tx_message, self.tx_time, self.tx_auth):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
        rows = (("Amount", self.tx_amount), ("Status", self.tx_status), ("Time", self.tx_time),
                ("Source", self.tx_source), ("Read by OCR", self.tx_read), ("Details", self.tx_message),
                ("Auth code", self.tx_auth))
        for row, (name, widget) in enumerate(rows):
            caption = QLabel(name + ":")
            caption.setStyleSheet("color: #888;")
            grid.addWidget(caption, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(widget, row, 1)
        self.open_last_button = QPushButton("Open screenshot")
        self.open_last_button.clicked.connect(self.open_last)
        self.open_json_button = QPushButton("Open details (.json)")
        self.open_json_button.clicked.connect(self.open_last_json)
        folder = QPushButton("Screenshots folder")
        folder.clicked.connect(self.open_folder)
        grid.addLayout(_row(self.open_last_button, self.open_json_button, folder), len(rows), 0, 1, 2)
        self.open_last_button.setEnabled(False)
        self.open_json_button.setEnabled(False)
        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _build_capture_page(self) -> QWidget:
        page, layout = self._page("Capture", "The screen area to capture and where the screenshots are saved.")

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
        select = QPushButton("Select area...")
        select.clicked.connect(self.select_area)
        show = QPushButton("Show area")
        show.clicked.connect(self.show_area)
        area_layout.addLayout(_row(select, show))
        layout.addWidget(area_box)

        output_box = QGroupBox("Screenshots")
        form = QFormLayout(output_box)
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText(str(config.Settings().screenshots_dir()))
        self.folder_edit.textChanged.connect(self._schedule_save)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_folder)
        open_folder = QPushButton("Open")
        open_folder.clicked.connect(self.open_folder)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        folder_row.addWidget(open_folder)
        form.addRow("Folder:", folder_row)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(DEFAULT_FILE_NAME)
        self.name_edit.textChanged.connect(self._schedule_save)
        form.addRow("File name:", self.name_edit)
        form.addRow("", _hint("Placeholders: {timestamp}, {date}, {time}.\n"
                              "A .json file with the amount and payment result is saved next to each screenshot."))
        self.clipboard_check = QCheckBox("Also copy the screenshot to the clipboard")
        self.clipboard_check.toggled.connect(self._schedule_save)
        self.notify_check = QCheckBox("Show a tray notification after each capture")
        self.notify_check.toggled.connect(self._schedule_save)
        form.addRow("", self.clipboard_check)
        form.addRow("", self.notify_check)
        layout.addWidget(output_box)
        layout.addStretch(1)
        return page

    def _build_payment_page(self) -> QWidget:
        page, layout = self._page("Payment", "After each capture the amount is read from the screenshot and sent "
                                             "to the card terminal.")

        ocr_box = QGroupBox("Read the amount (OCR)")
        form = QFormLayout(ocr_box)
        self.ocr_check = QCheckBox("Read the amount from each screenshot")
        self.ocr_check.toggled.connect(self._schedule_save)
        form.addRow("", self.ocr_check)
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText(DEFAULT_AMOUNT_LABEL)
        self.label_edit.setMaximumWidth(260)
        self.label_edit.textChanged.connect(self._schedule_save)
        form.addRow("Text before the amount:", self.label_edit)
        form.addRow("", _hint("The amount is the number after this text, on the same line or the next one."))
        test = QPushButton("Test: read the amount now")
        test.clicked.connect(self.test_ocr)
        self.ocr_result = QLabel()
        self.ocr_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.ocr_result.setWordWrap(True)
        form.addRow("", _row(test))
        form.addRow("", self.ocr_result)
        layout.addWidget(ocr_box)

        pay_box = QGroupBox("Card terminal")
        pay_form = QFormLayout(pay_box)
        self.payment_check = QCheckBox("Charge the amount and show the payment screen")
        self.payment_check.toggled.connect(self._schedule_save)
        pay_form.addRow("", self.payment_check)
        self.terminal_combo = QComboBox()
        for key, name in TERMINALS.items():
            self.terminal_combo.addItem(name, key)
        self.terminal_combo.currentIndexChanged.connect(self._schedule_save)
        pay_form.addRow("Terminal:", _row(self.terminal_combo))
        self.currency_edit = QLineEdit()
        self.currency_edit.setMaxLength(3)
        self.currency_edit.setMaximumWidth(70)
        self.currency_edit.setPlaceholderText("EUR")
        self.currency_edit.textChanged.connect(self._schedule_save)
        pay_form.addRow("Currency:", _row(self.currency_edit))
        self.sim_spin = QSpinBox()
        self.sim_spin.setRange(0, 30)
        self.sim_spin.setSuffix(" s")
        self.sim_spin.valueChanged.connect(self._schedule_save)
        pay_form.addRow("Simulated processing time:", _row(self.sim_spin))
        pay_form.addRow("", _hint("Shows \u201cProcessing payment...\u201d, then the result.\n"
                                  "Other captures are refused until the transaction is finished, so nothing is "
                                  "charged twice.\nReal terminals will be added here."))
        layout.addWidget(pay_box)

        after_box = QGroupBox("After a successful payment")
        after_form = QFormLayout(after_box)
        self.auto_close_check = QCheckBox("Close the success screen automatically after")
        self.auto_close_check.toggled.connect(self._schedule_save)
        self.success_spin = QSpinBox()
        self.success_spin.setRange(1, 30)
        self.success_spin.setSuffix(" s")
        self.success_spin.valueChanged.connect(self._schedule_save)
        after_form.addRow("", _row(self.auto_close_check, self.success_spin))
        self.tap_check = QCheckBox("Then press this POS button:")
        self.tap_check.toggled.connect(self._schedule_save)
        self.tap_label = QLabel()
        self.tap_label.setStyleSheet("font-weight: bold;")
        after_form.addRow("", _row(self.tap_check, self.tap_label))
        pick = QPushButton("Pick button...")
        pick.clicked.connect(self.pick_tap_point)
        show_point = QPushButton("Show")
        show_point.clicked.connect(self.show_tap_point)
        test_tap = QPushButton("Test tap")
        test_tap.clicked.connect(self.test_tap)
        after_form.addRow("", _row(pick, show_point, test_tap))
        self.tap_method_combo = QComboBox()
        for key, name in TAP_METHODS.items():
            self.tap_method_combo.addItem(name, key)
        self.tap_method_combo.currentIndexChanged.connect(self._schedule_save)
        after_form.addRow("Press it with:", _row(self.tap_method_combo))
        self.pick_hint = _hint("")
        after_form.addRow("", self.pick_hint)
        layout.addWidget(after_box)
        layout.addStretch(1)
        return page

    def _build_shortcuts_page(self) -> QWidget:
        page, layout = self._page("Shortcuts", "Work anywhere in Windows, also while the app is hidden in the tray.")
        box = QGroupBox("Keyboard shortcuts")
        form = QFormLayout(box)
        self.hotkey_edits: dict[str, QKeySequenceEdit] = {}
        for name, default in ((CAPTURE, DEFAULT_HOTKEY_CAPTURE), (SELECT, DEFAULT_HOTKEY_SELECT),
                              (PICK, DEFAULT_HOTKEY_TAP)):
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
            form.addRow(f"{name}:", row)
            self.hotkey_edits[name] = edit
        self.hotkey_status = QLabel()
        form.addRow("", self.hotkey_status)
        form.addRow("", _hint("Click a box and press the new keys; clear it to turn the shortcut off.\n"
                              "Use Ctrl, Alt or Win with a letter, digit or F-key. F1-F24, Print and Pause also work "
                              "alone."))
        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _build_service_page(self) -> QWidget:
        page, layout = self._page("Service", "How other programs call the app.")
        box = QGroupBox("Background service")
        form = QFormLayout(box)
        self.service_label = QLabel()
        self.service_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("Status:", self.service_label)
        self.http_check = QCheckBox("Local HTTP endpoint on 127.0.0.1, port")
        self.http_check.toggled.connect(self._on_http_changed)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.valueChanged.connect(self._on_http_changed)
        form.addRow("", _row(self.http_check, self.port_spin))
        self.update_check = QCheckBox("Check for updates at startup")
        self.update_check.toggled.connect(self._schedule_save)
        form.addRow("", self.update_check)
        form.addRow("", _hint(
            f"Call it with the keyboard shortcuts, {CLI_EXE_NAME}.exe (prints the saved path),\n"
            f"{EXE_NAME}.exe --capture, or GET http://127.0.0.1:<port>/capture or /capture.png."
        ))
        layout.addWidget(box)

        admin_box = QGroupBox("Administrator")
        admin_form = QFormLayout(admin_box)
        self.admin_label = QLabel()
        self.admin_label.setStyleSheet("font-weight: bold;")
        admin_form.addRow("Status:", self.admin_label)
        self.restart_admin_button = QPushButton("Restart as administrator")
        self.restart_admin_button.clicked.connect(self.restart_as_admin)
        admin_form.addRow("", _row(self.restart_admin_button))
        self.admin_startup_check = QCheckBox("Start with Windows as administrator (no permission prompt)")
        self.admin_startup_check.toggled.connect(self._on_admin_startup_toggled)
        admin_form.addRow("", self.admin_startup_check)
        admin_form.addRow("", _hint("Needed when the POS program runs as administrator: Windows blocks simulated "
                                    "taps\nfrom programs that don't. Restart as administrator once, then tick the "
                                    "box above\nso it always starts that way (a Task Scheduler task replaces the "
                                    "normal startup entry)."))
        layout.addWidget(admin_box)
        layout.addStretch(1)
        return page

    def _build_log_page(self) -> QWidget:
        page, layout = self._page("Activity log", "Every capture, amount read, payment step and setting change.")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setMinimumHeight(300)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.log_view, 1)
        open_file = QPushButton("Open log file")
        open_file.clicked.connect(self.open_log)
        open_folder = QPushButton("Open logs folder")
        open_folder.clicked.connect(lambda: self._open_path(self.log_file.parent if self.log_file else default_logs_dir()))
        layout.addLayout(_row(open_file, open_folder))
        layout.addWidget(_hint(f"Saved to {self.log_file or default_logs_dir()}; a new file each day, kept 90 days."))
        return page

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self._icon_off, self)
        menu = QMenu(self)
        bold = QAction("Capture now", self)
        font = bold.font()
        font.setBold(True)
        bold.setFont(font)
        bold.triggered.connect(lambda: self.capture_now("tray"))
        menu.addAction(bold)
        select = menu.addAction("Select area...", self.select_area)
        pick = menu.addAction("Pick POS button...", self.pick_tap_point)
        self._tray_actions = {CAPTURE: (bold, "Capture now"), SELECT: (select, "Select area..."),
                              PICK: (pick, "Pick POS button...")}
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
        self.hotkey_edits[PICK].setKeySequence(QKeySequence.fromString(s.hotkey_tap, PORTABLE))
        self.auto_close_check.setChecked(s.success_auto_close)
        self.success_spin.setValue(s.success_seconds)
        self.tap_check.setChecked(s.tap_enabled)
        self.tap_method_combo.setCurrentIndex(max(0, self.tap_method_combo.findData(s.tap_method)))
        self.ocr_check.setChecked(s.ocr_enabled)
        self.label_edit.setText("" if s.amount_label == DEFAULT_AMOUNT_LABEL else s.amount_label)
        self.payment_check.setChecked(s.payment_enabled)
        self.terminal_combo.setCurrentIndex(max(0, self.terminal_combo.findData(s.terminal)))
        self.currency_edit.setText(s.currency)
        self.sim_spin.setValue(s.simulated_seconds)
        self._loading = False
        self._apply_payment_state()
        self._update_area_label()
        self._update_admin_status()

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
        s.hotkey_tap = self.hotkey_edits[PICK].keySequence().toString(PORTABLE)
        s.success_auto_close = self.auto_close_check.isChecked()
        s.success_seconds = self.success_spin.value()
        s.tap_enabled = self.tap_check.isChecked()
        s.tap_method = self.tap_method_combo.currentData() or "touch"
        s.ocr_enabled = self.ocr_check.isChecked()
        s.amount_label = self.label_edit.text().strip() or DEFAULT_AMOUNT_LABEL
        s.payment_enabled = self.payment_check.isChecked()
        s.terminal = self.terminal_combo.currentData() or "simulated"
        s.currency = self.currency_edit.text().strip().upper() or "EUR"
        s.simulated_seconds = self.sim_spin.value()
        self._apply_payment_state()
        self._save_timer.start()

    def _apply_payment_state(self) -> None:
        s = self.settings
        for widget in (self.payment_check, self.terminal_combo, self.currency_edit, self.sim_spin, self.label_edit):
            widget.setEnabled(s.ocr_enabled)
        self.success_spin.setEnabled(s.success_auto_close)
        self.service.hold_after_capture = s.ocr_enabled and s.payment_enabled
        point = parse_point(s.tap_point)
        self.tap_label.setText(f"({point[0]}, {point[1]})" if point else "not picked yet")
        key = display(s.hotkey_tap) if s.hotkey_tap else ""
        self.pick_hint.setText(
            f"Pick button... (or {key} anywhere): tap the POS button once to save its position.\n"
            "Failed, declined or unread payments still wait for the operator to tap." if key else
            "Pick button...: tap the POS button once to save its position.\n"
            "Failed, declined or unread payments still wait for the operator to tap.")
        self._update_home()

    def _save_settings(self) -> None:
        current = asdict(self.settings)
        changes = [f"{key}={current[key]!r}" for key in current if key != "extra" and current[key] != self._saved.get(key)]
        try:
            config.save(self.settings)
        except OSError as exc:
            logging.error("Could not save settings: %s", exc)
            return
        if changes:
            logging.info("Settings changed: %s", ", ".join(changes))
        self._saved = current

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
        self._update_home()

    def _update_home(self) -> None:
        if not hasattr(self, "status_title"):
            return
        s = self.settings
        if self.service.busy:
            title, color, detail = "Transaction in progress", "#1565c0", "Waiting for the operator to finish on the payment screen."
        elif s.area is None:
            title, color, detail = "Select a capture area", "#ef6c00", "Click Select area... and drag over the amount on the POS screen."
        else:
            title, color = "Ready", "#2e7d32"
            action = "capture and take the payment" if s.ocr_enabled and s.payment_enabled else (
                "capture and read the amount" if s.ocr_enabled else "capture")
            key = display(s.hotkey_capture) if s.hotkey_capture else ""
            detail = f"Press {key} anywhere to {action}." if key else f"Click Capture now to {action}."
            if s.ocr_enabled and s.payment_enabled and s.tap_enabled and not parse_point(s.tap_point):
                pick = display(s.hotkey_tap) if s.hotkey_tap else "Payment > Pick button..."
                detail += f"\nNo POS button picked yet for after a successful payment (use {pick})."
        self.status_title.setText(title)
        self.status_title.setStyleSheet(f"color: {color};")
        self.status_detail.setText(detail)
        problem = getattr(self, "_tap_problem", "")
        self.problem_label.setText(f"The POS button could not be pressed: {problem}" if problem else "")
        self.problem_label.setVisible(bool(problem))
        self.problem_button.setVisible(bool(problem) and "administrator" in problem and not is_elevated())

    def _on_http_changed(self) -> None:
        if self._loading:
            return
        self._schedule_save()
        self._http_timer.start()

    def _restart_http(self) -> None:
        self.service.restart_http()
        self._update_status()

    def _update_status(self) -> None:
        parts = [f"Running. Pipe \\\\.\\pipe\\{pipe_name()}"]
        if self.service.http_running:
            parts.append(f"HTTP http://127.0.0.1:{self.settings.http_port}/")
        elif self.service.http_error:
            parts.append(f"HTTP off: {self.service.http_error}")
        self.service_label.setText("\n".join(parts))
        self.tray.setIcon(self._icon_on)
        self.tray.setToolTip(f"{APP_NAME} {__version__}: ready")

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Screenshots folder", str(self.settings.screenshots_dir()))
        if folder:
            self.folder_edit.setText(folder)

    # -- administrator --------------------------------------------------------

    def _update_admin_status(self) -> None:
        elevated = is_elevated()
        self.admin_label.setText("Running as administrator" if elevated else "Not running as administrator")
        self.admin_label.setStyleSheet(f"font-weight: bold; color: {'#2e7d32' if elevated else '#888'};")
        self.restart_admin_button.setVisible(not elevated)
        self._loading, was_loading = True, self._loading
        self.admin_startup_check.setChecked(startup_task_exists())
        self._loading = was_loading
        self.admin_startup_check.setEnabled(elevated)
        self.admin_startup_check.setToolTip("" if elevated else "Restart as administrator first.")

    def restart_as_admin(self) -> None:
        if self.service.busy:
            QMessageBox.information(self, APP_NAME, "Finish the current transaction first.")
            return
        args = ["--replace", str(os.getpid())] + ([] if self.isVisible() else ["--minimized"])
        error = relaunch_elevated(*args)
        if error == "cancelled":
            logging.info("Restart as administrator cancelled at the Windows prompt")
            return
        if error:
            logging.error("Could not restart as administrator: %s", error)
            QMessageBox.warning(self, APP_NAME, f"Could not restart as administrator ({error}).")
            return
        logging.info("Restarting as administrator")
        self.quit()

    def _on_admin_startup_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        error = set_startup_task(checked)
        if error:
            logging.error("Could not change the start-at-logon task: %s", error)
            QMessageBox.warning(self, APP_NAME, f"Could not change how the app starts with Windows.\n\n{error}")
        self._update_admin_status()

    def _check_tap_target(self, point: tuple[int, int]) -> None:
        owner = window_owner_at(*point)
        if owner is None:
            logging.warning("No window found at the POS button position (%d, %d)", *point)
            return
        admin = {True: "yes", False: "no", None: "unknown"}[owner.elevated]
        logging.info("POS button is in %s (runs as administrator: %s)", owner.name, admin)
        if owner.elevated and not is_elevated():
            self._tap_problem = (f"{owner.name} runs as administrator, so Windows blocks simulated taps from "
                                 "Swift Screen Reader. Run Swift Screen Reader as administrator (Service page).")
            self._update_home()
            box = QMessageBox(QMessageBox.Icon.Warning, APP_NAME,
                              f"{owner.name} runs as administrator.\n\nWindows only lets programs that also run as "
                              "administrator press its buttons. Restart Swift Screen Reader as administrator now?",
                              parent=self)
            restart = box.addButton("Restart as administrator", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is restart:
                self.restart_as_admin()
        else:
            self._tap_problem = ""
            self._update_home()

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
        shortcuts = {CAPTURE: self.settings.hotkey_capture, SELECT: self.settings.hotkey_select,
                     PICK: self.settings.hotkey_tap}
        errors = self.hotkeys.apply(shortcuts)
        active = [name for name, text in shortcuts.items() if text and name not in errors]
        if errors:
            self.hotkey_status.setStyleSheet("color: #c62828;")
            self.hotkey_status.setText("\n".join(errors.values()))
        elif active:
            self.hotkey_status.setStyleSheet("color: #2e7d32;")
            self.hotkey_status.setText("Active.")
        else:
            self.hotkey_status.setStyleSheet("color: #888;")
            self.hotkey_status.setText("No shortcuts set.")
        for name, (action, label) in self._tray_actions.items():
            text = shortcuts[name]
            action.setText(f"{label}\t{display(text)}" if text and name not in errors else label)
        self._update_home()

    def _on_hotkey(self, name: str) -> None:
        text = {CAPTURE: self.settings.hotkey_capture, SELECT: self.settings.hotkey_select,
                PICK: self.settings.hotkey_tap}[name]
        logging.info("Shortcut %s pressed: %s", display(text), name)
        if self._selector is not None or self._picker is not None or self.screen_cover.visible:
            logging.info("Shortcut ignored: %s", "payment screen is showing" if self.screen_cover.visible
                         else "a selection is open")
            return
        if name == CAPTURE:
            self.capture_now("shortcut")
        elif name == SELECT:
            self.select_area()
        elif name == PICK:
            self.pick_tap_point()

    # -- capture and transactions --------------------------------------------

    def start_service(self) -> bool:
        if not self.service.start():
            return False
        self._service_started = True
        self._update_status()
        self.apply_hotkeys()
        threading.Thread(target=ocr_warm_up, name="ocr-warm-up", daemon=True).start()
        return True

    def _on_command(self, cmd: str) -> None:
        logging.info("Command from the command line: %s", cmd)
        if cmd == "show":
            self.show_window()
        elif cmd == "select":
            self.select_area()
        elif cmd == "quit":
            QTimer.singleShot(0, self.quit)

    def capture_now(self, source: str = "button") -> None:
        if self.settings.area is None:
            logging.info("Capture (%s): no area selected yet, opening the area selection", source)
            self.select_area()
            return
        try:
            self.service.capture(source=source)
        except BusyError as exc:
            self.statusBar().showMessage(str(exc), 4000)
        except (CaptureError, OSError):
            pass  # already logged through the failed signal

    def _on_captured(self, path: str, source: str, area: str) -> None:
        self.last_path = path
        self.open_last_button.setEnabled(True)
        logging.info("Screenshot saved (source: %s, area: %s): %s", source, area, path)
        if self.settings.copy_to_clipboard:
            image = QImage(path)
            if not image.isNull():
                QApplication.clipboard().setImage(image)
        if self.settings.notify:
            self._tray_click = self.open_last
            self.tray.showMessage(APP_NAME, f"Saved {Path(path).name}", QSystemTrayIcon.MessageIcon.Information, 2000)
        if not self.settings.ocr_enabled:
            self.service.release()
            return
        tx = self.runner.start(path, source, area, self.settings)
        self.current_tx = tx
        if tx.payment:
            self.screen_cover.show_processing("Reading amount...")
            logging.info("TX %s: payment screen shown", tx.id)
        self._show_tx(tx)
        self._update_home()

    def _on_failed(self, error: str, source: str) -> None:
        logging.error("Capture failed (source: %s): %s", source, error)
        if self.settings.notify:
            self.tray.showMessage(APP_NAME, f"Capture failed: {error}", QSystemTrayIcon.MessageIcon.Warning, 4000)

    def _on_tx_changed(self, tx: Transaction) -> None:
        if tx.status == PAYING and tx.payment and self.screen_cover.visible:
            self.screen_cover.show_processing("Processing payment...", tx.amount_display)
        self._show_tx(tx)

    def _on_tx_finished(self, tx: Transaction) -> None:
        self.last_tx = tx
        self._show_tx(tx)
        if not (tx.payment and self.screen_cover.visible):
            self._end_transaction(tx)
            return
        if tx.status == APPROVED:
            auto = self.settings.success_seconds if self.settings.success_auto_close else None
            self.screen_cover.show_result(SUCCESS, "Transaction successful", tx.amount_display,
                                          "Continuing..." if auto else "Tap anywhere to continue", auto_close_seconds=auto)
        elif tx.status in (DECLINED, ERROR):
            self.screen_cover.show_result(FAILURE, "Transaction declined" if tx.status == DECLINED else "Transaction failed",
                                          tx.message)
        elif tx.status == ZERO:
            self.screen_cover.show_result(WARNING, "Nothing to pay", tx.message)
        else:
            self.screen_cover.show_result(WARNING, "Could not read the amount", tx.message)
        logging.info("TX %s: result screen shown (%s)", tx.id, STATUS_TEXT.get(tx.status, tx.status))

    def _on_result_dismissed(self, by: str) -> None:
        tx = self.current_tx
        if tx is None:
            return
        self.runner.dismiss(tx, by)
        point = parse_point(self.settings.tap_point)
        if tx.status == APPROVED and self.settings.tap_enabled and point:
            # Still busy until the tap is done, so a capture can't sneak in between.
            QTimer.singleShot(TAP_DELAY_MS, lambda: self._tap_pos_button(tx, point))
        else:
            self._end_transaction(tx)

    def _tap_pos_button(self, tx: Transaction, point: tuple[int, int]) -> None:
        text = f"{point[0]},{point[1]}"
        try:
            method, target = tap(*point, self.settings.tap_method)
        except TapError as exc:
            self.runner.record_tap(tx, text, self.settings.tap_method, str(exc))
            self._tap_problem = str(exc)
            if self.settings.notify or "administrator" in str(exc):
                self.tray.showMessage(APP_NAME, f"Could not press the POS button: {exc}",
                                      QSystemTrayIcon.MessageIcon.Warning, 8000)
        else:
            self.runner.record_tap(tx, text, method, target=target)
            self._tap_problem = ""
        self._end_transaction(tx)

    def _end_transaction(self, tx: Transaction) -> None:
        if self.current_tx is tx:
            self.current_tx = None
        self.service.release()
        self._update_home()
        if self.settings.notify and tx.status in STATUS_TEXT:
            self.tray.showMessage(APP_NAME, f"{STATUS_TEXT[tx.status]}: {tx.amount_display}",
                                  QSystemTrayIcon.MessageIcon.Information, 3000)

    def _show_tx(self, tx: Transaction) -> None:
        self.tx_amount.setText(tx.amount_display if tx.amount is not None else "-")
        status = STATUS_TEXT.get(tx.status, "Reading amount..." if not tx.done else tx.status)
        self.tx_status.setText(status)
        self.tx_status.setStyleSheet(f"font-size: 12pt; font-weight: bold; color: {STATUS_COLORS.get(tx.status, '#1565c0')};")
        self.tx_time.setText(tx.started.strftime("%Y-%m-%d %H:%M:%S"))
        self.tx_source.setText(tx.source)
        self.tx_read.setText(" | ".join(tx.ocr_lines) if tx.ocr_lines else "-")
        self.tx_message.setText(tx.message or "-")
        self.tx_auth.setText(tx.auth_code or "-")
        self.open_json_button.setEnabled(tx.done)

    def test_ocr(self) -> None:
        area = self.settings.area
        if area is None:
            self.ocr_result.setText("Select a capture area first.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = recognize(QImage.fromData(capture_png(area)), self.settings.amount_label)
        except (CaptureError, OcrError) as exc:
            self.ocr_result.setStyleSheet("color: #c62828;")
            self.ocr_result.setText(str(exc))
            logging.warning("OCR test failed: %s", exc)
            return
        finally:
            QApplication.restoreOverrideCursor()
        amount = format_amount(result.amount, self.settings.currency) if result.amount is not None else "not found"
        ok = result.amount is not None
        self.ocr_result.setStyleSheet(f"color: {'#2e7d32' if ok else '#c62828'};")
        self.ocr_result.setText(f"Amount: {amount}\nRead: {result.text or '(no text)'}  ({result.ms} ms)")
        logging.info("OCR test: read %r, amount %s (%d ms)", result.text, amount, result.ms)

    def select_area(self) -> None:
        if self._selector is not None:
            return
        was_visible = self.isVisible()
        self.hide()
        logging.info("Area selection opened")

        def done(area: Area | None) -> None:
            self._selector = None
            if area is not None:
                self._set_area(area)
                logging.info("Capture area set to %s", area)
                self.show_area()
            else:
                logging.info("Area selection cancelled")
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

    def pick_tap_point(self) -> None:
        if self._picker is not None or self._selector is not None or self.screen_cover.visible:
            return
        was_visible = self.isVisible()
        self.hide()
        logging.info("POS button picker opened")

        def done(point: tuple[int, int] | None) -> None:
            self._picker = None
            if point is not None:
                self.settings.tap_point = f"{point[0]},{point[1]}"
                if not self.tap_check.isChecked():
                    self.tap_check.setChecked(True)
                self._schedule_save()
                logging.info("POS button set to (%d, %d)", *point)
                self.show_tap_point()
                # Once the picker is gone, see which program owns the button.
                QTimer.singleShot(400, lambda: self._check_tap_target(point))
            else:
                logging.info("POS button picker cancelled")
            if was_visible:
                self.show_window()

        def start() -> None:
            self._picker = PointPicker("Tap the POS button to press after a successful payment.   Esc to cancel.", self)
            self._picker.finished.connect(done)
            self._picker.start()

        QTimer.singleShot(200, start)

    def show_tap_point(self) -> None:
        point = parse_point(self.settings.tap_point)
        if point is None:
            QMessageBox.information(self, APP_NAME, "No POS button picked yet.")
            return
        marker = PointHighlight(*point)
        if marker.valid:
            marker.show()
        else:
            marker.deleteLater()
            logging.warning("The POS button position (%d, %d) is not on any connected screen", *point)

    def test_tap(self) -> None:
        point = parse_point(self.settings.tap_point)
        if point is None:
            QMessageBox.information(self, APP_NAME, "Pick the POS button first.")
            return
        was_visible = self.isVisible()
        self.hide()  # the window may be covering the button

        def run() -> None:
            try:
                method, target = tap(*point, self.settings.tap_method)
                logging.info("Test tap at (%d, %d) in %s (%s)", point[0], point[1], target, method)
                self._tap_problem = ""
            except TapError as exc:
                logging.error("Test tap at (%d, %d) failed: %s", point[0], point[1], exc)
                self._tap_problem = str(exc)
            self._update_home()
            if was_visible:
                QTimer.singleShot(500, self.show_window)

        QTimer.singleShot(TAP_DELAY_MS + 200, run)

    def _open_path(self, path: str | Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_folder(self) -> None:
        folder = self.settings.screenshots_dir()
        folder.mkdir(parents=True, exist_ok=True)
        self._open_path(folder)

    def open_last(self) -> None:
        if self.last_path and Path(self.last_path).exists():
            self._open_path(self.last_path)

    def open_last_json(self) -> None:
        if self.last_tx and self.last_tx.json_path.exists():
            self._open_path(self.last_tx.json_path)

    def open_log(self) -> None:
        if self.log_file and self.log_file.exists():
            self._open_path(self.log_file)

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
        if self.service.busy:
            QMessageBox.information(self, "Update", "Finish the current transaction first.")
            return
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
            "<p>Captures a preselected screen area when called, reads the amount with OCR "
            "and takes the card payment.</p>"
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
        logging.info("Quitting")
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save_settings()
        self.service.stop()
        self.hotkeys.stop()
        self.screen_cover.hide()
        QApplication.instance().focusChanged.disconnect(self._on_focus_changed)
        self.tray.hide()
        QApplication.quit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=EXE_NAME, description=f"{APP_NAME} (system tray)")
    parser.add_argument("--minimized", action="store_true", help="start hidden in the system tray")
    parser.add_argument("--select", action="store_true", help="start by selecting the capture area")
    parser.add_argument("--capture", action="store_true", help="take a screenshot and exit (see the CLI for options)")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--replace", type=int, metavar="PID", help=argparse.SUPPRESS)
    args, qt_args = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    if args.replace:
        # Restarted (e.g. as administrator): let the old copy exit and release the pipe first.
        from .elevation import wait_for_exit

        wait_for_exit(args.replace, 15)

    # Single instance: hand the request to the running copy instead of starting another one.
    request = {"cmd": "select" if args.select else ("ping" if args.minimized else "show")}
    if ipc.request(request) is not None:
        return 0
    if not ipc.acquire_instance_lock():
        # Another copy is starting right now; pass the request on once it is listening.
        for _ in range(100):
            time.sleep(0.1)
            if ipc.request(request) is not None:
                break
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
    log_file = setup_file_logging()

    window = MainWindow(emitter, log_file)
    if not window.start_service():
        QMessageBox.critical(None, APP_NAME, f"{APP_NAME} could not open its pipe: another copy may be running.")
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
