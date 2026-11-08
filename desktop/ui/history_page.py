# desktop/ui/history_page.py
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from PySide6.QtCore import Qt


class HistoryPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(20)

        placeholder = QLabel("History page — coming in Message 4")
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setObjectName("statusLabel")
        layout.addWidget(placeholder)