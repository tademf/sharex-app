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

        # NOTE: Receive page's QR is for phone → Windows uploads (phone scans,
        # uploads to Windows). It contains Windows's IP, not the phone's.
        # Send page needs the phone's IP, so we do NOT auto-fill from the QR.
        # TODO Phase 5: implement bidirectional QR that carries both devices' info.

        style_path = Path(__file__).parent / "style.qss"
        if style_path.exists():
            self.setStyleSheet(style_path.read_text(encoding="utf-8"))

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

    # def _on_pairing_changed(self, ip: str, token: str):
    #     """Disabled in Phase 4 — see comment in __init__."""
    #     pass

    def closeEvent(self, event: QCloseEvent):
        try:
            self.receive_page.shutdown()
        except Exception:
            pass
        event.accept()