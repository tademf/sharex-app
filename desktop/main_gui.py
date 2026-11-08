# desktop/main_gui.py
"""
SHAREX Desktop — GUI entry point.

Launches the PySide6 window. The FastAPI server and the transfer queue
are attached to the window in later messages (Message 3 and Message 2).
For now, this just displays the shell: header, tabs, theme.
"""
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon, QFont

from ui.main_window import MainWindow


def main() -> int:
    # Ensure `ui` package is importable when running as a script
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))

    app = QApplication(sys.argv)
    app.setApplicationName("SHAREX")
    app.setOrganizationName("SHAREX")

    # Default font
    font = QFont("Segoe UI", 10)
    app.setFont(font)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())