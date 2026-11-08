# desktop/ui/chunk_uploader.py
"""
SHAREX v2 chunk protocol client (desktop sender side).

Pure HTTP — no Qt, no UI. Speaks the protocol defined in shared/protocol.md:

    POST /session/start              -> { session_id, chunks_needed, ... }
    POST /session/<id>/chunk/<idx>   -> { bytes_written, chunks_received, ... }
    POST /session/<id>/finish        -> { status, sha256, size_bytes }
    POST /session/<id>/cancel        -> { status: "cancelled" }

Pause/resume/cancel are driven by threading.Event objects passed into run().
"""
import hashlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests


DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB — matches shared/protocol.md
CHUNK_RETRY_ATTEMPTS = 3
CHUNK_RETRY_BASE_DELAY = 1.0  # seconds; doubles per attempt


class Paused(Exception):
    """Raised internally when the caller sets the pause event mid-transfer."""


class Cancelled(Exception):
    """Raised internally when the caller sets the cancel event mid-transfer."""


@dataclass
class UploadResult:
    session_id: str
    sha256: str
    size_bytes: int
    stored_filename: str


@dataclass
class UploadCallbacks:
    """All callbacks fire on the caller's thread (the worker thread)."""
    on_hashing: Callable[[], None]                    = lambda: None
    on_session_started: Callable[[str, int, int], None] = lambda sid, total, needed: None
    on_chunk_done: Callable[[int, int, int], None]    = lambda idx, done, total: None
    on_retry: Callable[[int, int, str], None]         = lambda idx, attempt, err: None


def compute_sha256(filepath: Path,
                   cancel_event: Optional[threading.Event] = None) -> str:
    """Whole-file SHA-256. Long operation — caller should treat as blocking."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            if cancel_event and cancel_event.is_set():
                raise Cancelled()
            buf = f.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _check_events(cancel: threading.Event, pause: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled()
    if pause.is_set():
        raise Paused()


def _post_chunk(url: str, token: str, chunk_bytes: bytes,
                cancel: threading.Event, pause: threading.Event,
                timeout: float = 120.0) -> dict:
    """POST a single chunk, retrying with exponential backoff on transient errors."""
    last_err: Optional[str] = None
    for attempt in range(1, CHUNK_RETRY_ATTEMPTS + 1):
        _check_events(cancel, pause)
        try:
            resp = requests.post(
                url,
                data=chunk_bytes,
                headers={
                    "X-Pairing-Token": token,
                    "Content-Type": "application/octet-stream",
                },
                timeout=timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            # 4xx — non-retryable (bad token, bad index, session gone)
            if 400 <= resp.status_code < 500:
                raise RuntimeError(
                    f"HTTP {resp.status_code}: {resp.text[:200]}"
                )
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"

        if attempt < CHUNK_RETRY_ATTEMPTS:
            delay = CHUNK_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            # Sleep in small increments so cancel/pause stay responsive
            slept = 0.0
            while slept < delay:
                _check_events(cancel, pause)
                time.sleep(0.1)
                slept += 0.1
    raise RuntimeError(f"chunk failed after {CHUNK_RETRY_ATTEMPTS} attempts: {last_err}")


def upload_file(
    filepath: Path,
    server_ip: str,
    token: str,
    resume_id: Optional[str] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    callbacks: Optional[UploadCallbacks] = None,
    cancel_event: Optional[threading.Event] = None,
    pause_event: Optional[threading.Event] = None,
) -> UploadResult:
    """
    Upload `filepath` to `server_ip` using the v2 chunk protocol.

    Raises:
        Cancelled     — cancel_event was set
        Paused        — pause_event was set (caller should resume with resume_id)
        RuntimeError  — protocol or network failure after retries
    """
    cb = callbacks or UploadCallbacks()
    cancel = cancel_event or threading.Event()
    pause = pause_event or threading.Event()

    total_size = filepath.stat().st_size
    filename = filepath.name
    base_url = f"http://{server_ip}:8080"

    # --- Step 1: hash the whole file ---------------------------------------
    cb.on_hashing()
    sha256 = compute_sha256(filepath, cancel_event=cancel)
    _check_events(cancel, pause)

    # --- Step 2: start (or resume) the session -----------------------------
    start_payload = {
        "filename": filename,
        "total_size": total_size,
        "sha256": sha256,
        "chunk_size": chunk_size,
        "resume_id": resume_id,   # None for fresh
    }
    start_resp = requests.post(
        f"{base_url}/session/start",
        json=start_payload,
        headers={"X-Pairing-Token": token},
        timeout=30.0,
    )
    if start_resp.status_code != 200:
        raise RuntimeError(
            f"/session/start failed: HTTP {start_resp.status_code}: "
            f"{start_resp.text[:200]}"
        )
    start_data = start_resp.json()
    session_id = start_data["session_id"]
    chunks_needed = list(start_data.get("chunks_needed", []))
    total_chunks = (total_size + chunk_size - 1) // chunk_size

    cb.on_session_started(session_id, total_chunks, len(chunks_needed))

    # --- Step 3: upload each needed chunk ----------------------------------
    chunks_done = 0
    with open(filepath, "rb") as f:
        for idx in chunks_needed:
            _check_events(cancel, pause)

            offset = idx * chunk_size
            f.seek(offset)
            chunk_bytes = f.read(chunk_size)
            if not chunk_bytes:
                raise RuntimeError(f"file truncated at chunk {idx}")

            url = f"{base_url}/session/{session_id}/chunk/{idx}"

            # Wrap retry notifications through the callback
            original_post = _post_chunk

            def _post_with_notify(u, t, cb_bytes, c, p, timeout=120.0):
                return original_post(
                    u, t, cb_bytes, c, p,
                    timeout=timeout,
                )

            try:
                _post_with_notify(url, token, chunk_bytes, cancel, pause)
            except RuntimeError as e:
                # Surface retry count via callback before re-raising
                cb.on_retry(idx, CHUNK_RETRY_ATTEMPTS, str(e))
                raise

            chunks_done += 1
            cb.on_chunk_done(idx, chunks_done, len(chunks_needed))

    # --- Step 4: finish ----------------------------------------------------
    _check_events(cancel, pause)

    finish_resp = requests.post(
        f"{base_url}/session/{session_id}/finish",
        json={"sha256": sha256},
        headers={"X-Pairing-Token": token},
        timeout=60.0,
    )
    if finish_resp.status_code == 422:
        raise RuntimeError(f"hash mismatch on receiver: {finish_resp.text[:300]}")
    if finish_resp.status_code != 200:
        raise RuntimeError(
            f"/session/finish failed: HTTP {finish_resp.status_code}: "
            f"{finish_resp.text[:200]}"
        )

    finish_data = finish_resp.json()
    if finish_data.get("status") != "success":
        raise RuntimeError(f"finish returned non-success: {finish_data}")

    return UploadResult(
        session_id=session_id,
        sha256=finish_data.get("sha256", sha256),
        size_bytes=finish_data.get("size_bytes", total_size),
        stored_filename=finish_data.get("stored_filename", filename),
    )


def cancel_session(server_ip: str, token: str, session_id: str) -> bool:
    """Best-effort cancel. Returns True if the server acknowledged."""
    try:
        resp = requests.post(
            f"http://{server_ip}:8080/session/{session_id}/cancel",
            headers={"X-Pairing-Token": token},
            timeout=10.0,
        )
        return resp.status_code == 200
    except requests.RequestException:
        return False