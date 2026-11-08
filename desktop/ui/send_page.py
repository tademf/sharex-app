# desktop/ui/send_page.py
"""
Send page: drag-and-drop files, queue them, upload to a paired receiver.

Uses the same HTTP protocol as `sender/client.py`, but integrated into the
GUI so the user doesn't touch the terminal. Sends POST multipart requests
to http://<ip>:8000/upload with the X-Pairing-Token header.

Each file becomes a `TransferJob` on a background thread. Progress updates
come back to the UI thread via Qt signals, so the UI never freezes.
"""
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog,
    QFrame, QProgressBar, QLineEdit, QScrollArea, QSizePolicy
)

from database import record_transfer


@dataclass
class TransferJob:
    filepath: Path
    size: int
    status: str = "queued"          # queued | uploading | success | failed
    progress: int = 0               # 0-100
    speed_bps: float = 0.0
    error: Optional[str] = None
    sha256: Optional[str] = None
    row_widget: Optional[QWidget] = None
    progress_bar: Optional[QProgressBar] = None
    status_label: Optional[QLabel] = None


class UploadWorker(QThread):
    """Runs a single file upload off the UI thread."""
    progress = Signal(int)          # percent 0-100
    finished_ok = Signal(str)       # sha256
    failed = Signal(str)            # error message

    def __init__(self, filepath: Path, server_ip: str, token: str):
        super().__init__()
        self.filepath = filepath
        self.server_ip = server_ip
        self.token = token

    def run(self):
        try:
            sha = self._upload()
            self.finished_ok.emit(sha)
        except Exception as e:
            self.failed.emit(str(e))

    def _upload(self) -> str:
        url = f"http://{self.server_ip}:8080/upload"
        headers = {
            "X-Pairing-Token": self.token,
            "X-Filename": self.filepath.name,
        }
        size = self.filepath.stat().st_size
        sent = [0]
        start = [time.time()]

        class ProgressFile:
            def __init__(self, path):
                self.f = open(path, "rb")
                self.total = size

            def read(self, n=-1):
                chunk = self.f.read(n)
                if chunk:
                    sent[0] += len(chunk)
                    pct = int(sent[0] * 100 / self.total) if self.total else 0
                    self.cb(pct)
                return chunk

            def __len__(self):
                return self.total

            def close(self):
                self.f.close()

            cb = lambda self, pct: None  # placeholder, set later

        prog_file = ProgressFile(self.filepath)
        prog_file.cb = lambda pct: self.progress.emit(pct)

        try:
            resp = requests.post(
                url,
                files={"file": (self.filepath.name, prog_file, "application/octet-stream")},
                headers=headers,
                timeout=600,
            )
        finally:
            prog_file.close()

        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        if "sha256" not in data:
            raise RuntimeError(f"Missing sha256 in response: {data}")
        return data["sha256"]


class SendPage(QWidget):
    def __init__(self):
        super().__init__()

        self.jobs: list[TransferJob] = []
        self.workers: list[UploadWorker] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(16)

        # --- Section title ---
        title = QLabel("Send files to paired device")
        title.setObjectName("sectionLabel")
        root.addWidget(title)

        hint = QLabel("Drag files here, or click to browse. Files send automatically to the phone you paired with.")
        hint.setObjectName("statusLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # --- Target (peer) input row ---
        target_row = QHBoxLayout()
        target_row.setSpacing(12)

        target_label = QLabel("Target:")
        target_label.setObjectName("statusLabel")

        self.server_ip_input = QLineEdit()
        self.server_ip_input.setPlaceholderText("Windows IP on hotspot — leave blank to send to Windows server")  # unused for Send
        self.server_ip_input.setVisible(False)  # Send tab targets the phone receiver, not the Windows server

        # For Phase 4 Send, we target the *phone receiver* at 8080.
        # The pair flow (Message 3) sets this. For now, expose manual input.
        self.peer_ip_input = QLineEdit()
        self.peer_ip_input.setPlaceholderText("Phone IP (e.g. 192.168.137.238)")
        self.peer_ip_input.setText("192.168.137.238")

        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText("Pairing token")
        self.token_input.setEchoMode(QLineEdit.Password)
        # Default to the phone's SimpleHttpServer token (Phase 2 default)
        self.token_input.setText("sharex-phase2-test")
        target_row.addWidget(target_label)
        target_row.addWidget(self.peer_ip_input, 2)
        target_row.addWidget(self.token_input, 2)
        root.addLayout(target_row)
        target_note = QLabel(
            "These are the phone's values. The phone's IP on the hotspot is "
            "shown in the phone's SHAREX receiver screen."
        )
        target_note.setObjectName("statusLabel")
        target_note.setWordWrap(True)
        target_note.setStyleSheet("font-size: 11px; color: #7A8CA8;")
        root.addWidget(target_note)
        # --- Drop zone ---
        self.drop_zone = QFrame()
        self.drop_zone.setObjectName("dropZone")
        self.drop_zone.setProperty("dragging", False)
        self.drop_zone.setAcceptDrops(True)
        self.drop_zone.setMinimumHeight(180)
        self.drop_zone.installEventFilter(self)

        dz_layout = QVBoxLayout(self.drop_zone)
        dz_layout.setAlignment(Qt.AlignCenter)
        dz_layout.setSpacing(12)

        dz_icon = QLabel("↓")
        dz_icon.setStyleSheet("font-size: 48px; color: #1F6FFF;")
        dz_icon.setAlignment(Qt.AlignCenter)
        dz_layout.addWidget(dz_icon)

        dz_label = QLabel("Drop files here")
        dz_label.setStyleSheet("font-size: 16px; font-weight: 600; color: #E8EEF9;")
        dz_label.setAlignment(Qt.AlignCenter)
        dz_layout.addWidget(dz_label)

        dz_hint = QLabel("or click the button below to browse")
        dz_hint.setObjectName("statusLabel")
        dz_hint.setAlignment(Qt.AlignCenter)
        dz_layout.addWidget(dz_hint)

        root.addWidget(self.drop_zone)

        # Handle drag/drop manually
        self.drop_zone.dragEnterEvent = self._drag_enter
        self.drop_zone.dragLeaveEvent = self._drag_leave
        self.drop_zone.dropEvent = self._drop

        # --- Browse button ---
        browse_btn = QPushButton("Browse Files…")
        browse_btn.setFixedHeight(44)
        browse_btn.clicked.connect(self._browse_files)
        root.addWidget(browse_btn)

        # --- Queue header ---
        queue_header = QHBoxLayout()
        queue_label = QLabel("Transfer queue")
        queue_label.setObjectName("sectionLabel")
        queue_header.addWidget(queue_label)
        queue_header.addStretch(1)

        self.clear_btn = QPushButton("Clear completed")
        self.clear_btn.setObjectName("secondaryButton")
        self.clear_btn.clicked.connect(self._clear_completed)
        self.clear_btn.setFixedHeight(32)
        queue_header.addWidget(self.clear_btn)
        root.addLayout(queue_header)

        # --- Queue list (scrollable) ---
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background-color: transparent; }")

        self.queue_container = QWidget()
        self.queue_layout = QVBoxLayout(self.queue_container)
        self.queue_layout.setContentsMargins(0, 0, 0, 0)
        self.queue_layout.setSpacing(8)
        self.queue_layout.addStretch(1)

        scroll.setWidget(self.queue_container)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(scroll, 1)

    # --- Drag/drop event handlers ---

    def _drag_enter(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.drop_zone.setProperty("dragging", True)
            self.drop_zone.style().unpolish(self.drop_zone)
            self.drop_zone.style().polish(self.drop_zone)

    def _drag_leave(self, event):
        self.drop_zone.setProperty("dragging", False)
        self.drop_zone.style().unpolish(self.drop_zone)
        self.drop_zone.style().polish(self.drop_zone)

    def _drop(self, event: QDropEvent):
        self.drop_zone.setProperty("dragging", False)
        self.drop_zone.style().unpolish(self.drop_zone)
        self.drop_zone.style().polish(self.drop_zone)

        paths: list[Path] = []
        for url in event.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_file():
                paths.append(p)
        if paths:
            self._enqueue(paths)
        event.acceptProposedAction()

    def _browse_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select files to send")
        if files:
            self._enqueue([Path(f) for f in files])

    # --- Enqueue & run ---

    def _enqueue(self, paths: list[Path]):
        ip = self.peer_ip_input.text().strip()
        token = self.token_input.text().strip()

        if not ip:
            self._show_error("Target phone IP is empty")
            return
        if not token:
            self._show_error("Pairing token is empty — pair via the Receive tab first")
            return

        for path in paths:
            job = TransferJob(
                filepath=path,
                size=path.stat().st_size,
            )
            self.jobs.append(job)
            row = self._build_row(job)
            self.queue_layout.insertWidget(self.queue_layout.count() - 1, row)
            self._start_worker(job, ip, token)

    def _start_worker(self, job: TransferJob, ip: str, token: str):
        worker = UploadWorker(job.filepath, ip, token)
        self.workers.append(worker)

        def on_progress(pct: int):
            job.progress = pct
            if job.progress_bar:
                job.progress_bar.setValue(pct)

        def on_ok(sha: str):
            job.status = "success"
            job.sha256 = sha
            job.progress = 100
            if job.progress_bar:
                job.progress_bar.setValue(100)
            if job.status_label:
                job.status_label.setText("✓ Success")
                job.status_label.setObjectName("successLabel")
                job.status_label.style().unpolish(job.status_label)
                job.status_label.style().polish(job.status_label)
            record_transfer(
                direction="sent",
                filename=job.filepath.name,
                size_bytes=job.size,
                sha256=sha,
                peer_ip=ip,
                status="success",
            )

        def on_fail(err: str):
            job.status = "failed"
            job.error = err
            if job.status_label:
                job.status_label.setText(f"✗ {err[:60]}")
                job.status_label.setObjectName("errorLabel")
                job.status_label.style().unpolish(job.status_label)
                job.status_label.style().polish(job.status_label)
            record_transfer(
                direction="sent",
                filename=job.filepath.name,
                size_bytes=job.size,
                peer_ip=ip,
                status="failed",
                error=err[:500],
            )

        worker.progress.connect(on_progress)
        worker.finished_ok.connect(on_ok)
        worker.failed.connect(on_fail)
        worker.finished.connect(lambda: self.workers.remove(worker) if worker in self.workers else None)
        worker.start()

    # --- UI helpers ---

    def _build_row(self, job: TransferJob) -> QWidget:
        row = QFrame()
        row.setObjectName("card")
        row.setStyleSheet("QFrame#card { background-color: #131E30; }")

        layout = QHBoxLayout(row)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        name = QLabel(job.filepath.name)
        name.setStyleSheet("font-weight: 600; color: #E8EEF9;")
        name.setMinimumWidth(200)
        layout.addWidget(name, 2)

        size = QLabel(_fmt_size(job.size))
        size.setObjectName("statusLabel")
        size.setFixedWidth(80)
        layout.addWidget(size)

        bar = QProgressBar()
        bar.setMinimum(0)
        bar.setMaximum(100)
        bar.setValue(0)
        bar.setFixedHeight(12)
        layout.addWidget(bar, 3)

        status = QLabel("Queued")
        status.setObjectName("statusLabel")
        status.setFixedWidth(140)
        layout.addWidget(status)

        job.row_widget = row
        job.progress_bar = bar
        job.status_label = status
        return row

    def _clear_completed(self):
        for job in self.jobs[:]:
            if job.status in ("success", "failed") and job.row_widget is not None:
                job.row_widget.setParent(None)
                self.jobs.remove(job)

    def _show_error(self, msg: str):
        label = QLabel(msg)
        label.setObjectName("errorLabel")
        self.queue_layout.insertWidget(self.queue_layout.count() - 1, label)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"