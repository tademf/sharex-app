# desktop/ui/settings_page.py
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from PySide6.QtCore import Qt


class SettingsPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(20)

        placeholder = QLabel("Settings page — coming in Message 4")
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setObjectName("statusLabel")
        layout.addWidget(placeholder)