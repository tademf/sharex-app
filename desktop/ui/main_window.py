# desktop/ui/main_window.py
from pathlib import Path

from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QVBoxLayout, QLabel, QHBoxLayout,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent

from ui.send_page import SendPage
from ui.receive_page import ReceivePage
from ui.history_page import HistoryPage
from ui.settings_page import SettingsPage
from ui.tray import TrayIcon


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("SHAREX — Your Files. Your Devices. No Internet.")
        self.setMinimumSize(900, 700)
        self.resize(1100, 780)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = self._build_header()
        layout.addWidget(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        self.send_page = SendPage()
        self.receive_page = ReceivePage()
        self.history_page = HistoryPage()
        self.settings_page = SettingsPage()

        self.tabs.addTab(self.send_page, "Send")
        self.tabs.addTab(self.receive_page, "Receive")
        self.tabs.addTab(self.history_page, "History")
        self.tabs.addTab(self.settings_page, "Settings")
        layout.addWidget(self.tabs, 1)

        # Note: Receive page's QR is for phone → Windows uploads. Its IP is
        # the laptop's IP, not the phone's, so we don't auto-fill SendPage.

        style_path = Path(__file__).parent / "style.qss"
        if style_path.exists():
            self.setStyleSheet(style_path.read_text(encoding="utf-8"))

        # System tray
        self.tray = TrayIcon(self)

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setFixedHeight(76)
        header.setStyleSheet("background-color: #0B1420;")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(28, 16, 28, 16)
        layout.setSpacing(12)

        title = QLabel("SHAREX")
        title.setObjectName("titleLabel")

        subtitle = QLabel("Your Files. Your Devices. No Internet.")
        subtitle.setObjectName("subtitleLabel")
        subtitle.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch(1)

        return header

    def closeEvent(self, event: QCloseEvent):
        try:
            self.receive_page.shutdown()
        except Exception:
            pass
        try:
            self.tray.hide()
        except Exception:
            pass
        event.accept()