# desktop/ui/history_page.py
"""
History tab: shows every past transfer stored in the SQLite database.
Columns: Date, Direction, Filename, Size, Device, SHA-256 (truncated).
Refreshes when the tab is shown.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView,
)
from PySide6.QtGui import QColor

from database import recent_transfers


class HistoryPage(QWidget):
    def __init__(self):
        super().__init__()
        self._build_ui()
        self._load_rows()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(16)

        title = QLabel("Transfer history")
        title.setObjectName("sectionLabel")
        root.addWidget(title)

        hint = QLabel("Every transfer sent or received through SHAREX, stored locally.")
        hint.setObjectName("statusLabel")
        root.addWidget(hint)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["Date", "Direction", "Filename", "Size", "Device", "SHA-256"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        root.addWidget(self.table, 1)

        # Refresh button
        refresh_row = QHBoxLayout()
        refresh_row.addStretch(1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setObjectName("secondaryButton")
        refresh_btn.setFixedHeight(32)
        refresh_btn.clicked.connect(self._load_rows)
        refresh_row.addWidget(refresh_btn)
        root.addLayout(refresh_row)

    def _load_rows(self):
        rows = recent_transfers(limit=200)
        self.table.setRowCount(len(rows))

        for i, row in enumerate(rows):
            direction = row.get("direction", "")
            status = row.get("status", "")

            date_item = QTableWidgetItem(row.get("created_at", "")[:19].replace("T", " "))
            self.table.setItem(i, 0, date_item)

            dir_item = QTableWidgetItem(
                "↑ Sent" if direction == "sent" else "↓ Received"
            )
            self.table.setItem(i, 1, dir_item)

            self.table.setItem(i, 2, QTableWidgetItem(row.get("filename", "")))

            size_bytes = row.get("size_bytes", 0) or 0
            self.table.setItem(i, 3, QTableWidgetItem(_fmt_size(size_bytes)))

            peer = row.get("peer_ip") or row.get("device_name") or "—"
            self.table.setItem(i, 4, QTableWidgetItem(peer))

            sha = row.get("sha256") or "—"
            sha_item = QTableWidgetItem(sha[:16] + "…" if len(sha) > 16 else sha)
            sha_item.setToolTip(sha)
            self.table.setItem(i, 5, sha_item)

            if status == "failed":
                for col in range(6):
                    itm = self.table.item(i, col)
                    if itm:
                        itm.setForeground(QColor("#D93654"))

    def showEvent(self, event):
        super().showEvent(event)
        self._load_rows()


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"