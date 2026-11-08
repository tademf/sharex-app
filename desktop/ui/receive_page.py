# desktop/ui/receive_page.py
"""
Receive page: starts the embedded FastAPI server, displays the QR code
live so the phone can scan it, and shows status of received files.

The QR is drawn directly using the `qrcode` library + QPixmap, so the
user never opens a browser tab.
"""
import io
import json
import time
from pathlib import Path
from typing import Optional

import qrcode
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
)

from ui.server_runner import ServerRunner
from receiver.server import local_ip_on_hotspot
from receiver.token_manager import TokenManager
from database import record_transfer

SIGNAL_PATH = Path(__file__).resolve().parent.parent / "last_received.json"


class ReceivePage(QWidget):
    pairing_changed = Signal(str, str)  # (ip, token)

    def __init__(self):
        super().__init__()

        self.server = ServerRunner(host="0.0.0.0", port=8000)
        self.tokens = TokenManager(ttl_seconds=300)
        self.current_token: Optional[str] = None
        self.current_expiry: float = 0.0
        self.last_signal_mtime: float = 0.0

        self._build_ui()
        self._start_server()

        # Timer: update countdown + auto-refresh QR
        self.qr_refresh_timer = QTimer(self)
        self.qr_refresh_timer.timeout.connect(self._maybe_refresh_qr)
        self.qr_refresh_timer.start(1000)

        # Timer: poll for received-file signal
        self.signal_poll_timer = QTimer(self)
        self.signal_poll_timer.timeout.connect(self._poll_for_received)
        self.signal_poll_timer.start(500)

        self._regenerate_qr()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(16)

        title = QLabel("Receive files from phone")
        title.setObjectName("sectionLabel")
        root.addWidget(title)

        hint = QLabel(
            "Scan this QR with the SHAREX app on your phone. "
            "The IP and pairing token will transfer automatically."
        )
        hint.setObjectName("statusLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)

        columns = QHBoxLayout()
        columns.setSpacing(32)

        qr_frame = QFrame()
        qr_frame.setObjectName("card")
        qr_frame.setFixedSize(340, 340)
        qr_layout = QVBoxLayout(qr_frame)
        qr_layout.setAlignment(Qt.AlignCenter)

        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setFixedSize(300, 300)
        qr_layout.addWidget(self.qr_label)

        columns.addWidget(qr_frame)

        info_col = QVBoxLayout()
        info_col.setSpacing(12)

        self.status_label = QLabel("Starting server…")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        info_col.addWidget(self.status_label)

        self.ip_label = QLabel("IP: —")
        info_col.addWidget(self.ip_label)

        self.port_label = QLabel("Port: 8000")
        info_col.addWidget(self.port_label)

        self.expires_label = QLabel("Expires in: —")
        info_col.addWidget(self.expires_label)

        info_col.addSpacing(12)

        regen_btn = QPushButton("Regenerate QR")
        regen_btn.setObjectName("secondaryButton")
        regen_btn.setFixedHeight(40)
        regen_btn.clicked.connect(self._regenerate_qr)
        info_col.addWidget(regen_btn)

        info_col.addStretch(1)
        columns.addLayout(info_col, 1)
        root.addLayout(columns)

        root.addSpacing(16)
        recent_label = QLabel("Recent received")
        recent_label.setObjectName("sectionLabel")
        root.addWidget(recent_label)

        self.recent_status = QLabel("No files received yet.")
        self.recent_status.setObjectName("statusLabel")
        self.recent_status.setWordWrap(True)
        root.addWidget(self.recent_status)

        root.addStretch(1)

    def _start_server(self):
        ok = self.server.start()
        if ok:
            self.status_label.setText("Server running — waiting for phone scan.")
            self.status_label.setObjectName("successLabel")
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)
        else:
            self.status_label.setText("Server failed to start on port 8000")
            self.status_label.setObjectName("errorLabel")
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)

    def _regenerate_qr(self):
        token, expires = self.tokens.issue()
        self.current_token = token
        self.current_expiry = expires

        ip = local_ip_on_hotspot()
        payload = {
            "v": 1,
            "device": "SHAREX-Receiver",
            "ip": ip,
            "port": 8000,
            "token": token,
            "expires": int(expires),
        }
        data = json.dumps(payload)

        qr = qrcode.QRCode(
            version=4,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        qimg = QImage.fromData(buf.getvalue(), "PNG")
        pix = QPixmap.fromImage(qimg).scaled(
            300, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.qr_label.setPixmap(pix)

        self.ip_label.setText(f"IP: {ip}")
        self.port_label.setText("Port: 8000")
        self._update_expiry_label()

        self.pairing_changed.emit(ip, token)

    def _update_expiry_label(self):
        remaining = int(self.current_expiry - time.time())
        if remaining <= 0:
            self.expires_label.setText("Expires in: EXPIRED")
            self.expires_label.setObjectName("errorLabel")
        else:
            mins = remaining // 60
            secs = remaining % 60
            self.expires_label.setText(f"Expires in: {mins}:{secs:02d}")
            self.expires_label.setObjectName("statusLabel")
        self.expires_label.style().unpolish(self.expires_label)
        self.expires_label.style().polish(self.expires_label)

    def _maybe_refresh_qr(self):
        self._update_expiry_label()
        if time.time() > self.current_expiry:
            self._regenerate_qr()

    def _poll_for_received(self):
        if not SIGNAL_PATH.exists():
            return
        mtime = SIGNAL_PATH.stat().st_mtime
        if mtime <= self.last_signal_mtime:
            return
        self.last_signal_mtime = mtime

        try:
            data = json.loads(SIGNAL_PATH.read_text(encoding="utf-8"))
            filename = data.get("filename", "unknown")
            size = data.get("size_bytes", 0)
            sha = data.get("sha256", "")
            self.recent_status.setText(
                f"✓ Received: {filename}  ({_fmt_size(size)})  sha256: {sha[:16]}…"
            )
            self.recent_status.setObjectName("successLabel")
            self.recent_status.style().unpolish(self.recent_status)
            self.recent_status.style().polish(self.recent_status)

            record_transfer(
                direction="received",
                filename=filename,
                size_bytes=size,
                sha256=sha,
                status="success",
            )
        except Exception:
            pass

    def shutdown(self):
        self.qr_refresh_timer.stop()
        self.signal_poll_timer.stop()
        self.server.stop()


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"