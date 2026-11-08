# desktop/ui/settings_page.py
"""
Settings tab: download folder, server port, device name, QR refresh interval.
Values are persisted to desktop/settings.json.
"""
import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QFileDialog, QMessageBox,
)

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.json"


DEFAULTS = {
    "download_dir": str(Path.home() / "Downloads" / "SHAREX"),
    "server_port": 8000,
    "device_name": "SHAREX-Receiver",
    "qr_refresh_seconds": 300,
}


def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            merged = {**DEFAULTS, **data}
            return merged
        except Exception:
            return dict(DEFAULTS)
    return dict(DEFAULTS)


def save_settings(data: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


class SettingsPage(QWidget):
    def __init__(self):
        super().__init__()
        self.values = load_settings()
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(20)

        title = QLabel("Settings")
        title.setObjectName("sectionLabel")
        root.addWidget(title)

        hint = QLabel("Changes are saved when you click Save.")
        hint.setObjectName("statusLabel")
        root.addWidget(hint)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(14)

        # Download folder
        grid.addWidget(QLabel("Download folder"), 0, 0)
        self.download_dir_input = QLineEdit(self.values["download_dir"])
        grid.addWidget(self.download_dir_input, 0, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.setObjectName("secondaryButton")
        browse_btn.setFixedWidth(110)
        browse_btn.clicked.connect(self._pick_folder)
        grid.addWidget(browse_btn, 0, 2)

        # Server port
        grid.addWidget(QLabel("Server port"), 1, 0)
        self.port_input = QSpinBox()
        self.port_input.setRange(1024, 65535)
        self.port_input.setValue(self.values["server_port"])
        grid.addWidget(self.port_input, 1, 1)

        # Device name
        grid.addWidget(QLabel("Device name"), 2, 0)
        self.device_name_input = QLineEdit(self.values["device_name"])
        grid.addWidget(self.device_name_input, 2, 1)

        # QR refresh interval
        grid.addWidget(QLabel("QR refresh (seconds)"), 3, 0)
        self.qr_refresh_input = QSpinBox()
        self.qr_refresh_input.setRange(60, 3600)
        self.qr_refresh_input.setSingleStep(30)
        self.qr_refresh_input.setValue(self.values["qr_refresh_seconds"])
        grid.addWidget(self.qr_refresh_input, 3, 1)

        root.addLayout(grid)

        # Save button
        actions = QHBoxLayout()
        actions.addStretch(1)
        save_btn = QPushButton("Save")
        save_btn.setFixedHeight(40)
        save_btn.clicked.connect(self._save)
        actions.addWidget(save_btn)
        root.addLayout(actions)

        root.addStretch(1)

    def _pick_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select download folder",
            self.download_dir_input.text() or str(Path.home()),
        )
        if folder:
            self.download_dir_input.setText(folder)

    def _save(self):
        data = {
            "download_dir": self.download_dir_input.text().strip() or DEFAULTS["download_dir"],
            "server_port": self.server_port_int(),
            "device_name": self.device_name_input.text().strip() or DEFAULTS["device_name"],
            "qr_refresh_seconds": self.qr_refresh_input.value(),
        }
        try:
            save_settings(data)
            self.values = data
            QMessageBox.information(
                self, "Settings saved",
                "Settings saved. Some changes take effect after restart."
            )
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))

    def server_port_int(self) -> int:
        return int(self.port_input.value())