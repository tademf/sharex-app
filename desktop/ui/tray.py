# desktop/ui/tray.py
"""
System tray icon for SHAREX.
Minimize to tray, click to restore, right-click for menu.
"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QAction, QPixmap, QPainter, QColor, QFont
from PySide6.QtWidgets import QSystemTrayIcon, QMenu, QStyle


def _generate_icon() -> QIcon:
    """Draw a simple 'S' icon programmatically if no icon file exists."""
    icon_path = Path(__file__).resolve().parent / "resources" / "icon.png"
    if icon_path.exists():
        return QIcon(str(icon_path))

    # Fallback: draw a navy circle with a cyan S
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor("#0F1B2E"))
    painter.setPen(QColor("#00D9FF"))
    painter.drawEllipse(2, 2, 60, 60)
    painter.setPen(QColor("#00D9FF"))
    painter.setFont(QFont("Segoe UI", 32, QFont.Bold))
    painter.drawText(pix.rect(), Qt.AlignCenter, "S")
    painter.end()
    return QIcon(pix)


class TrayIcon(QSystemTrayIcon):
    def __init__(self, main_window):
        self.main_window = main_window
        super().__init__(_generate_icon(), main_window)

        self.setToolTip("SHAREX — Your Files. Your Devices. No Internet.")

        # Menu
        menu = QMenu()
        show_action = QAction("Show SHAREX", menu)
        show_action.triggered.connect(self._show_window)
        menu.addAction(show_action)

        menu.addSeparator()

        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)

        # Click behavior
        self.activated.connect(self._on_activated)

        self.show()

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger or reason == QSystemTrayIcon.DoubleClick:
            self._show_window()

    def _show_window(self):
        self.main_window.showNormal()
        self.main_window.raise_()
        self.main_window.activateWindow()

    def _quit(self):
        self.main_window.close()

    def notify(self, title: str, body: str):
        if self.supportsMessages():
            self.showMessage(title, body, QSystemTrayIcon.Information, 3000)