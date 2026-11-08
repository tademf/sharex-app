# desktop/ui/send_page.py
"""
Send page: drag-and-drop files, queue them, upload to the paired phone.

Uses the v2 chunk protocol from shared/protocol.md via chunk_uploader.py.
Transfers run on a ThreadPoolExecutor (max 3 concurrent). Progress is
marshaled to the GUI thread through Qt signals emitted from worker threads.

Phase 5 additions:
  - Per-row Pause / Resume / Cancel / Retry buttons
  - Concurrent transfers (configurable max, default 3)
  - Resume across app restarts via session_id in database
  - Chunk-level progress (4 MiB granularity, not per-byte)
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog,
    QFrame, QProgressBar, QLineEdit, QScrollArea, QSizePolicy,
)

from database import (
    record_transfer,
    update_transfer_session,
    resumable_transfers,
)
from ui.chunk_uploader import (
    upload_file, cancel_session, UploadCallbacks,
    Cancelled, Paused,
)


MAX_CONCURRENT = 3


@dataclass
class TransferJob:
    filepath: Path
    size: int
    status: str = "queued"          # queued | hashing | uploading | paused | success | failed | cancelled
    progress: int = 0               # 0-100
    error: Optional[str] = None
    sha256: Optional[str] = None
    session_id: Optional[str] = None
    db_row_id: Optional[int] = None
    chunks_done: int = 0
    chunks_total: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)
    # UI bindings
    row_widget: Optional[QWidget] = None
    progress_bar: Optional[QProgressBar] = None
    status_label: Optional[QLabel] = None
    pause_btn: Optional[QPushButton] = None
    cancel_btn: Optional[QPushButton] = None
    retry_btn: Optional[QPushButton] = None


class JobSignals(QObject):
    """Signals emitted from worker threads, received on GUI thread."""
    hashing = Signal(str)                          # job_id
    session_started = Signal(str, str, int, int)   # job_id, session_id, total, needed
    chunk_done = Signal(str, int, int)             # job_id, done, total
    ok = Signal(str, str, str, int)                # job_id, sha256, stored_name, size
    failed = Signal(str, str)                      # job_id, error
    paused = Signal(str)                           # job_id
    cancelled = Signal(str)                        # job_id


class SendPage(QWidget):
    def __init__(self):
        super().__init__()

        self.jobs: dict[str, TransferJob] = {}
        self.pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)
        self.signals = JobSignals()
        self._wire_signals()

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(16)

        title = QLabel("Send files to paired device")
        title.setObjectName("sectionLabel")
        root.addWidget(title)

        hint = QLabel(
            "Drag files here or click browse. Up to 3 transfers run at once. "
            "Pause / resume / retry per file."
        )
        hint.setObjectName("statusLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # Peer + token row
        target_row = QHBoxLayout()
        target_row.setSpacing(12)
        target_label = QLabel("Target:")
        target_label.setObjectName("statusLabel")
        self.peer_ip_input = QLineEdit()
        self.peer_ip_input.setPlaceholderText("Phone IP (e.g. 192.168.137.238)")
        self.peer_ip_input.setText("192.168.137.238")
        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText("Pairing token")
        self.token_input.setEchoMode(QLineEdit.Password)
        self.token_input.setText("sharex-phase2-test")
        target_row.addWidget(target_label)
        target_row.addWidget(self.peer_ip_input, 2)
        target_row.addWidget(self.token_input, 2)
        root.addLayout(target_row)

        # Resume banner (hidden unless unfinished sessions exist)
        self.resume_banner = QFrame()
        self.resume_banner.setStyleSheet(
            "QFrame { background-color: #1A2A44; border-radius: 6px; }"
        )
        rb_layout = QHBoxLayout(self.resume_banner)
        rb_layout.setContentsMargins(16, 10, 16, 10)
        self.resume_label = QLabel("")
        self.resume_label.setStyleSheet("color: #E8EEF9;")
        rb_layout.addWidget(self.resume_label, 1)
        self.resume_all_btn = QPushButton("Resume all")
        self.resume_all_btn.setObjectName("secondaryButton")
        self.resume_all_btn.clicked.connect(self._resume_all_paused)
        rb_layout.addWidget(self.resume_all_btn)
        self.resume_banner.setVisible(False)
        root.addWidget(self.resume_banner)

        # Drop zone
        self.drop_zone = QFrame()
        self.drop_zone.setObjectName("dropZone")
        self.drop_zone.setProperty("dragging", False)
        self.drop_zone.setAcceptDrops(True)
        self.drop_zone.setMinimumHeight(160)
        dz_layout = QVBoxLayout(self.drop_zone)
        dz_layout.setAlignment(Qt.AlignCenter)
        dz_layout.setSpacing(12)
        dz_icon = QLabel("↓")
        dz_icon.setStyleSheet("font-size: 44px; color: #1F6FFF;")
        dz_icon.setAlignment(Qt.AlignCenter)
        dz_layout.addWidget(dz_icon)
        dz_label = QLabel("Drop files here")
        dz_label.setStyleSheet("font-size: 16px; font-weight: 600; color: #E8EEF9;")
        dz_label.setAlignment(Qt.AlignCenter)
        dz_layout.addWidget(dz_label)
        dz_hint = QLabel("or click the button below")
        dz_hint.setObjectName("statusLabel")
        dz_hint.setAlignment(Qt.AlignCenter)
        dz_layout.addWidget(dz_hint)
        self.drop_zone.dragEnterEvent = self._drag_enter
        self.drop_zone.dragLeaveEvent = self._drag_leave
        self.drop_zone.dropEvent = self._drop
        root.addWidget(self.drop_zone)

        # Browse
        browse_btn = QPushButton("Browse Files…")
        browse_btn.setFixedHeight(44)
        browse_btn.clicked.connect(self._browse_files)
        root.addWidget(browse_btn)

        # Queue header
        queue_header = QHBoxLayout()
        queue_label = QLabel("Transfer queue")
        queue_label.setObjectName("sectionLabel")
        queue_header.addWidget(queue_label)
        queue_header.addStretch(1)
        self.clear_btn = QPushButton("Clear completed")
        self.clear_btn.setObjectName("secondaryButton")
        self.clear_btn.setFixedHeight(32)
        self.clear_btn.clicked.connect(self._clear_completed)
        queue_header.addWidget(self.clear_btn)
        root.addLayout(queue_header)

        # Scrollable queue
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

        # Pick up unfinished sessions from prior app runs
        self._load_resumable()

    # --- Signal wiring -----------------------------------------------------

    def _wire_signals(self):
        self.signals.hashing.connect(self._on_hashing)
        self.signals.session_started.connect(self._on_session_started)
        self.signals.chunk_done.connect(self._on_chunk_done)
        self.signals.ok.connect(self._on_ok)
        self.signals.failed.connect(self._on_failed)
        self.signals.paused.connect(self._on_paused)
        self.signals.cancelled.connect(self._on_cancelled)

    # --- Drag/drop ---------------------------------------------------------

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
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls()]
        paths = [p for p in paths if p.is_file()]
        if paths:
            self._enqueue(paths)
        event.acceptProposedAction()

    def _browse_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select files to send")
        if files:
            self._enqueue([Path(f) for f in files])

    # --- Enqueue -----------------------------------------------------------

    def _enqueue(self, paths: list[Path], resume_id: Optional[str] = None,
                 db_row_id: Optional[int] = None):
        ip = self.peer_ip_input.text().strip()
        token = self.token_input.text().strip()
        if not ip:
            self._show_error("Target IP is empty")
            return
        if not token:
            self._show_error("Pairing token is empty")
            return

        for path in paths:
            job = TransferJob(
                filepath=path,
                size=path.stat().st_size,
                session_id=resume_id,
                db_row_id=db_row_id,
            )
            if db_row_id is None:
                job.db_row_id = record_transfer(
                    direction="sent",
                    filename=path.name,
                    size_bytes=job.size,
                    peer_ip=ip,
                    status="queued",
                    session_id=None,
                )
            job_id = f"job-{id(job)}"
            self.jobs[job_id] = job
            row = self._build_row(job, job_id)
            self.queue_layout.insertWidget(self.queue_layout.count() - 1, row)
            self._start_job(job_id, job, ip, token)

    def _start_job(self, job_id: str, job: TransferJob, ip: str, token: str):
        job.status = "queued"
        job.cancel_event.clear()
        job.pause_event.clear()
        if job.pause_btn:
            job.pause_btn.setText("Pause")
            job.pause_btn.setEnabled(True)
            job.pause_btn.setVisible(True)
        if job.cancel_btn:
            job.cancel_btn.setVisible(True)
        if job.retry_btn:
            job.retry_btn.setVisible(False)

        def worker():
            cb = UploadCallbacks(
                on_hashing=lambda: self.signals.hashing.emit(job_id),
                on_session_started=lambda sid, total, needed: self.signals.session_started.emit(
                    job_id, sid, total, needed
                ),
                on_chunk_done=lambda idx, done, total: self.signals.chunk_done.emit(
                    job_id, done, total
                ),
            )
            try:
                result = upload_file(
                    filepath=job.filepath,
                    server_ip=ip,
                    token=token,
                    resume_id=job.session_id,
                    callbacks=cb,
                    cancel_event=job.cancel_event,
                    pause_event=job.pause_event,
                )
                self.signals.ok.emit(
                    job_id, result.sha256, result.stored_filename, result.size_bytes
                )
            except Cancelled:
                self.signals.cancelled.emit(job_id)
            except Paused:
                self.signals.paused.emit(job_id)
            except Exception as e:
                self.signals.failed.emit(job_id, str(e))

        self.pool.submit(worker)

    # --- Signal handlers (GUI thread) --------------------------------------

    def _on_hashing(self, job_id: str):
        job = self.jobs.get(job_id)
        if not job:
            return
        job.status = "hashing"
        self._set_status(job, "Hashing…", "statusLabel")
        self._set_progress(job, 0)

    def _on_session_started(self, job_id: str, session_id: str,
                            total_chunks: int, needed_chunks: int):
        job = self.jobs.get(job_id)
        if not job:
            return
        job.session_id = session_id
        job.chunks_total = total_chunks
        job.status = "uploading"
        label = f"Uploading 0/{needed_chunks} chunks"
        if needed_chunks < total_chunks:
            label = f"Resuming ({total_chunks - needed_chunks}/{total_chunks} done)"
        self._set_status(job, label, "statusLabel")
        if job.db_row_id:
            update_transfer_session(
                job.db_row_id, session_id=session_id, status="uploading"
            )

    def _on_chunk_done(self, job_id: str, done: int, total: int):
        job = self.jobs.get(job_id)
        if not job:
            return
        job.chunks_done = done
        pct = int(done * 100 / total) if total else 0
        self._set_progress(job, pct)
        self._set_status(job, f"Uploading {done}/{total} chunks", "statusLabel")

    def _on_ok(self, job_id: str, sha256: str, stored_name: str, size: int):
        job = self.jobs.get(job_id)
        if not job:
            return
        job.status = "success"
        job.sha256 = sha256
        self._set_progress(job, 100)
        self._set_status(job, "✓ Success", "successLabel")
        if job.pause_btn:
            job.pause_btn.setVisible(False)
        if job.cancel_btn:
            job.cancel_btn.setVisible(False)
        if job.db_row_id:
            update_transfer_session(
                job.db_row_id, status="success", chunks_received=[]
            )

    def _on_failed(self, job_id: str, err: str):
        job = self.jobs.get(job_id)
        if not job:
            return
        job.status = "failed"
        job.error = err
        self._set_status(job, f"✗ {err[:60]}", "errorLabel")
        if job.pause_btn:
            job.pause_btn.setVisible(False)
        if job.retry_btn:
            job.retry_btn.setVisible(True)
        if job.db_row_id:
            update_transfer_session(job.db_row_id, status="failed", error=err[:500])

    def _on_paused(self, job_id: str):
        job = self.jobs.get(job_id)
        if not job:
            return
        job.status = "paused"
        self._set_status(job, "⏸ Paused", "statusLabel")
        if job.pause_btn:
            job.pause_btn.setText("Resume")
            job.pause_btn.setEnabled(True)      # fix: re-enable after pause
        if job.db_row_id:
            update_transfer_session(
                job.db_row_id, status="paused",
                chunks_received=list(range(job.chunks_done)),
            )

    def _on_cancelled(self, job_id: str):
        job = self.jobs.get(job_id)
        if not job:
            return
        job.status = "cancelled"
        self._set_status(job, "Cancelled", "errorLabel")
        if job.pause_btn:
            job.pause_btn.setVisible(False)
        if job.cancel_btn:
            job.cancel_btn.setVisible(False)
        if job.retry_btn:
            job.retry_btn.setVisible(True)
        if job.session_id:
            ip = self.peer_ip_input.text().strip()
            token = self.token_input.text().strip()
            cancel_session(ip, token, job.session_id)
        if job.db_row_id:
            update_transfer_session(job.db_row_id, status="cancelled")

    # --- Row UI ------------------------------------------------------------

    def _build_row(self, job: TransferJob, job_id: str) -> QWidget:
        row = QFrame()
        row.setObjectName("card")
        row.setStyleSheet("QFrame#card { background-color: #131E30; }")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        name = QLabel(job.filepath.name)
        name.setStyleSheet("font-weight: 600; color: #E8EEF9;")
        name.setMinimumWidth(180)
        layout.addWidget(name, 2)

        size = QLabel(_fmt_size(job.size))
        size.setObjectName("statusLabel")
        size.setFixedWidth(70)
        layout.addWidget(size)

        bar = QProgressBar()
        bar.setMinimum(0)
        bar.setMaximum(100)
        bar.setValue(0)
        bar.setFixedHeight(12)
        layout.addWidget(bar, 3)

        status = QLabel("Queued")
        status.setObjectName("statusLabel")
        status.setFixedWidth(150)
        layout.addWidget(status)

        pause_btn = QPushButton("Pause")
        pause_btn.setObjectName("secondaryButton")
        pause_btn.setFixedWidth(84)
        pause_btn.clicked.connect(lambda: self._toggle_pause(job_id))
        layout.addWidget(pause_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("secondaryButton")
        cancel_btn.setFixedWidth(84)
        cancel_btn.clicked.connect(lambda: self._cancel(job_id))
        layout.addWidget(cancel_btn)

        retry_btn = QPushButton("Retry")
        retry_btn.setObjectName("secondaryButton")
        retry_btn.setFixedWidth(84)
        retry_btn.setVisible(False)
        retry_btn.clicked.connect(lambda: self._retry(job_id))
        layout.addWidget(retry_btn)

        job.row_widget = row
        job.progress_bar = bar
        job.status_label = status
        job.pause_btn = pause_btn
        job.cancel_btn = cancel_btn
        job.retry_btn = retry_btn
        return row

    # --- User actions ------------------------------------------------------

    def _toggle_pause(self, job_id: str):
        job = self.jobs.get(job_id)
        if not job:
            return
        if job.status == "paused":
            # Resume: reuse stored session_id
            job.pause_event.clear()
            job.pause_btn.setText("Pause")
            job.pause_btn.setEnabled(True)
            self._set_status(job, "Resuming…", "statusLabel")
            ip = self.peer_ip_input.text().strip()
            token = self.token_input.text().strip()
            self._start_job(job_id, job, ip, token)
        else:
            job.pause_event.set()
            job.pause_btn.setEnabled(False)
            self._set_status(job, "Pausing…", "statusLabel")
            # _on_paused will re-enable the button and set text to "Resume"

    def _cancel(self, job_id: str):
        job = self.jobs.get(job_id)
        if not job:
            return
        job.cancel_event.set()

    def _retry(self, job_id: str):
        job = self.jobs.get(job_id)
        if not job:
            return
        if job.retry_btn:
            job.retry_btn.setVisible(False)
        if job.pause_btn:
            job.pause_btn.setVisible(True)
            job.pause_btn.setText("Pause")
            job.pause_btn.setEnabled(True)
        self._set_status(job, "Retrying…", "statusLabel")
        ip = self.peer_ip_input.text().strip()
        token = self.token_input.text().strip()
        self._start_job(job_id, job, ip, token)

    def _resume_all_paused(self):
        count = 0
        for job_id, job in self.jobs.items():
            if job.status in ("paused", "failed") and job.session_id:
                self._retry(job_id)
                count += 1
        if count == 0:
            self.resume_banner.setVisible(False)

    def _clear_completed(self):
        for job_id in list(self.jobs.keys()):
            job = self.jobs[job_id]
            if job.status in ("success", "failed", "cancelled") and job.row_widget:
                job.row_widget.setParent(None)
                del self.jobs[job_id]
        if not any(j.status in ("paused", "failed") for j in self.jobs.values()):
            self.resume_banner.setVisible(False)

    # --- Resumable sessions on startup -------------------------------------

    def _load_resumable(self):
        try:
            rows = resumable_transfers()
        except Exception:
            return
        if not rows:
            return
        self.resume_label.setText(
            f"{len(rows)} unfinished transfer(s) from previous session. "
            "Click 'Resume all' to continue."
        )
        self.resume_banner.setVisible(True)
        for r in rows:
            path = Path(r.get("filename", ""))
            job = TransferJob(
                filepath=path,
                size=r.get("size_bytes", 0),
                status="paused",
                session_id=r.get("session_id"),
                db_row_id=r.get("id"),
            )
            job_id = f"resume-{r.get('id')}"
            self.jobs[job_id] = job
            row = self._build_row(job, job_id)
            self._set_status(job, "⏸ Paused (prior session)", "statusLabel")
            if job.pause_btn:
                job.pause_btn.setText("Resume")
                job.pause_btn.setEnabled(True)
            self.queue_layout.insertWidget(self.queue_layout.count() - 1, row)

    # --- UI helpers --------------------------------------------------------

    def _set_status(self, job: TransferJob, text: str, obj_name: str):
        if not job.status_label:
            return
        job.status_label.setText(text)
        job.status_label.setObjectName(obj_name)
        job.status_label.style().unpolish(job.status_label)
        job.status_label.style().polish(job.status_label)

    def _set_progress(self, job: TransferJob, pct: int):
        job.progress = pct
        if job.progress_bar:
            job.progress_bar.setValue(pct)

    def _show_error(self, msg: str):
        label = QLabel(msg)
        label.setObjectName("errorLabel")
        self.queue_layout.insertWidget(self.queue_layout.count() - 1, label)

    def shutdown(self):
        """Clean shutdown — cancel events, wait for pool."""
        for job in self.jobs.values():
            job.cancel_event.set()
        self.pool.shutdown(wait=False, cancel_futures=True)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"