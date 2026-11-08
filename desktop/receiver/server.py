# desktop/receiver/server.py
"""
SHAREX Windows receiver.

v1: POST /upload              multipart, backward-compat (Phase 2 client.py)
v2: POST /session/start       begin/resume a chunked transfer
    POST /session/<id>/chunk/<idx>   upload a single 4 MiB chunk
    POST /session/<id>/finish        assemble + SHA-256 verify + move to downloads
    POST /session/<id>/cancel        delete chunks

Sessions live in memory (dict + asyncio.Lock). Chunks live on disk under
downloads/.sessions/<session_id>/chunk_XXXXX. TTL = 1 hour.
"""
import asyncio
import hashlib
import io
import json
import shutil
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import qrcode
from fastapi import FastAPI, File, UploadFile, HTTPException, Header, Request
from fastapi.responses import JSONResponse, Response

from receiver.token_manager import TokenManager

app = FastAPI(title="SHAREX Receiver", version="0.4.0")

DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

SESSIONS_DIR = DOWNLOAD_DIR / ".sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

PAIRING_TOKEN_TTL = 300  # 5 minutes
tokens = TokenManager(ttl_seconds=PAIRING_TOKEN_TTL)

CHUNK_SIZE_DEFAULT = 4 * 1024 * 1024  # 4 MiB
SESSION_TTL_SECONDS = 3600            # 1 hour
LEGACY_CHUNK_SIZE = 1024 * 1024       # 1 MiB, v1 multipart streaming
TRANSFER_PORT = 8000


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

@dataclass
class SessionState:
    session_id: str
    filename: str
    total_size: int
    chunk_size: int
    total_chunks: int
    expected_sha256: str
    created_at: float
    last_activity: float
    chunks_received: set = field(default_factory=set)
    cancelled: bool = False


_sessions: dict[str, SessionState] = {}
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def sanitize_filename(filename: str | None) -> str:
    if not filename:
        return f"{uuid.uuid4().hex}.bin"
    safe = Path(filename).name
    for ch in ':*?"<>|':
        safe = safe.replace(ch, "_")
    return f"{uuid.uuid4().hex}_{safe}"


def _plain_filename(filename: str | None) -> str:
    """Sanitize but don't prefix a UUID — used for stored_filename construction."""
    if not filename:
        return "unknown.bin"
    safe = Path(filename).name
    for ch in ':*?"<>|':
        safe = safe.replace(ch, "_")
    return safe[:120]


def local_ip_on_hotspot() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("192.168.137.1", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _write_signal(original_name: str, stored_name: str,
                  size: int, sha256: str) -> None:
    try:
        signal_path = DOWNLOAD_DIR.parent / "last_received.json"
        signal_path.write_text(json.dumps({
            "filename": original_name,
            "stored_filename": stored_name,
            "size_bytes": size,
            "sha256": sha256,
            "received_at": time.time(),
        }), encoding="utf-8")
    except Exception:
        pass


def _require_token(x_pairing_token: Optional[str]) -> str:
    if not x_pairing_token:
        raise HTTPException(status_code=403, detail="Missing X-Pairing-Token header")
    return x_pairing_token


# ---------------------------------------------------------------------------
# Existing endpoints (unchanged behavior)
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "alive",
        "service": "SHAREX Windows Receiver",
        "protocol": "v2",
    }


@app.get("/pairing")
async def issue_pairing():
    token, expires = tokens.issue()
    tokens.cleanup_expired()
    return {
        "v": 1,
        "device": "SHAREX-Receiver",
        "ip": local_ip_on_hotspot(),
        "port": TRANSFER_PORT,
        "token": token,
        "expires": int(expires),
        "ttl_seconds": PAIRING_TOKEN_TTL,
    }


@app.get("/pairing/qr.png")
async def pairing_qr():
    token, expires = tokens.issue()
    tokens.cleanup_expired()
    payload = {
        "v": 1,
        "device": "SHAREX-Receiver",
        "ip": local_ip_on_hotspot(),
        "port": TRANSFER_PORT,
        "token": token,
        "expires": int(expires),
    }
    data = json.dumps(payload)
    qr = qrcode.QRCode(
        version=4,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png")


@app.get("/pairing/status")
async def pairing_status():
    return {"active_tokens": tokens.active_count(), "ttl_seconds": PAIRING_TOKEN_TTL}


# ---------------------------------------------------------------------------
# v1 — legacy single-POST upload (backward compat)
# ---------------------------------------------------------------------------

@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    x_pairing_token: str = Header(...),
):
    if not tokens.validate_and_consume(x_pairing_token):
        raise HTTPException(status_code=403, detail="Invalid or expired pairing token")

    final_name = sanitize_filename(file.filename)
    dest_path = DOWNLOAD_DIR / final_name

    sha256 = hashlib.sha256()
    total_bytes = 0

    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = await file.read(LEGACY_CHUNK_SIZE)
                if not chunk:
                    break
                sha256.update(chunk)
                out.write(chunk)
                total_bytes += len(chunk)
        await file.close()

        _write_signal(file.filename, final_name, total_bytes, sha256.hexdigest())

        return JSONResponse({
            "status": "success",
            "original_filename": file.filename,
            "stored_filename": final_name,
            "size_bytes": total_bytes,
            "sha256": sha256.hexdigest(),
        })
    except Exception as e:
        if dest_path.exists():
            dest_path.unlink()
        raise HTTPException(status_code=500, detail=f"Transfer failed: {str(e)}")


# ---------------------------------------------------------------------------
# v2 — chunked session protocol
# ---------------------------------------------------------------------------

@app.post("/session/start")
async def session_start(request: Request):
    token = _require_token(request.headers.get("x-pairing-token"))
    if not tokens.validate_and_consume(token):
        raise HTTPException(status_code=403, detail="Invalid or expired pairing token")

    body = await request.json()
    filename = body.get("filename", "unknown.bin")
    total_size = int(body.get("total_size", 0))
    chunk_size = int(body.get("chunk_size", CHUNK_SIZE_DEFAULT))
    expected_sha = body.get("sha256", "") or ""
    resume_id = body.get("resume_id") or None

    if total_size <= 0 or chunk_size <= 0:
        raise HTTPException(status_code=400, detail="invalid total_size or chunk_size")

    total_chunks = (total_size + chunk_size - 1) // chunk_size

    # --- Resume path ---
    if resume_id:
        async with _lock:
            existing = _sessions.get(resume_id)
            if (
                existing is not None
                and not existing.cancelled
                and (time.time() - existing.last_activity) < SESSION_TTL_SECONDS
            ):
                existing.last_activity = time.time()
                missing = [i for i in range(existing.total_chunks)
                           if i not in existing.chunks_received]
                received = sorted(existing.chunks_received)
                return JSONResponse({
                    "session_id": resume_id,
                    "chunks_needed": missing,
                    "chunks_received": received,
                    "resume_token": resume_id,
                    "expires_at": int(existing.last_activity + SESSION_TTL_SECONDS),
                })

    # --- New session ---
    session_id = uuid.uuid4().hex
    state = SessionState(
        session_id=session_id,
        filename=filename,
        total_size=total_size,
        chunk_size=chunk_size,
        total_chunks=total_chunks,
        expected_sha256=expected_sha,
        created_at=time.time(),
        last_activity=time.time(),
    )
    async with _lock:
        _sessions[session_id] = state

    session_dir = SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    try:
        (session_dir / "meta.json").write_text(json.dumps({
            "session_id": session_id,
            "filename": filename,
            "total_size": total_size,
            "chunk_size": chunk_size,
            "total_chunks": total_chunks,
            "sha256": expected_sha,
        }), encoding="utf-8")
    except Exception:
        pass

    return JSONResponse({
        "session_id": session_id,
        "chunks_needed": list(range(total_chunks)),
        "resume_token": session_id,
        "expires_at": int(state.last_activity + SESSION_TTL_SECONDS),
    })


@app.post("/session/{session_id}/chunk/{index}")
async def session_chunk(session_id: str, index: int, request: Request):
    _require_token(request.headers.get("x-pairing-token"))

    async with _lock:
        state = _sessions.get(session_id)
        if state is None:
            raise HTTPException(status_code=404, detail="session not found")
        if state.cancelled:
            raise HTTPException(status_code=409, detail="session cancelled")
        if index < 0 or index >= state.total_chunks:
            raise HTTPException(status_code=400, detail="chunk index out of range")
        chunk_size_limit = state.chunk_size

    body = await request.body()
    if len(body) > chunk_size_limit:
        raise HTTPException(status_code=400, detail="chunk too large")

    session_dir = SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    chunk_file = session_dir / f"chunk_{index:05d}"

    try:
        chunk_file.write_bytes(body)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"chunk write failed: {e}")

    async with _lock:
        state = _sessions.get(session_id)
        if state is not None:
            state.chunks_received.add(index)
            state.last_activity = time.time()
            received_count = len(state.chunks_received)
            total = state.total_chunks
        else:
            received_count, total = 1, 1

    return JSONResponse({
        "index": index,
        "bytes_written": len(body),
        "chunks_received": received_count,
        "chunks_total": total,
    })


@app.post("/session/{session_id}/finish")
async def session_finish(session_id: str, request: Request):
    _require_token(request.headers.get("x-pairing-token"))

    async with _lock:
        state = _sessions.get(session_id)
        if state is None:
            raise HTTPException(status_code=404, detail="session not found")
        if state.cancelled:
            raise HTTPException(status_code=409, detail="session cancelled")
        if len(state.chunks_received) < state.total_chunks:
            missing = sorted(set(range(state.total_chunks)) - state.chunks_received)
            raise HTTPException(
                status_code=400,
                detail=f"missing chunks: {missing[:20]}",
            )

    # Optional client-supplied hash override
    try:
        finish_body = await request.json()
    except Exception:
        finish_body = {}
    client_sha = (finish_body or {}).get("sha256", "") or ""

    session_dir = SESSIONS_DIR / session_id
    safe_name = _plain_filename(state.filename)
    final_name = f"{uuid.uuid4().hex}_{safe_name}"
    final_path = DOWNLOAD_DIR / final_name

    sha256 = hashlib.sha256()
    total = 0

    try:
        with open(final_path, "wb") as out:
            for i in range(state.total_chunks):
                chunk_path = session_dir / f"chunk_{i:05d}"
                if not chunk_path.exists():
                    raise HTTPException(
                        status_code=500,
                        detail=f"chunk {i} missing on disk",
                    )
                with open(chunk_path, "rb") as inp:
                    while True:
                        buf = inp.read(64 * 1024)
                        if not buf:
                            break
                        sha256.update(buf)
                        out.write(buf)
                        total += len(buf)
    except HTTPException:
        if final_path.exists():
            final_path.unlink()
        raise
    except Exception as e:
        if final_path.exists():
            final_path.unlink()
        raise HTTPException(status_code=500, detail=f"assembly failed: {e}")

    actual_sha = sha256.hexdigest()
    expected = state.expected_sha256 or client_sha
    if expected and expected.lower() != actual_sha.lower():
        try:
            final_path.unlink()
        except Exception:
            pass
        shutil.rmtree(session_dir, ignore_errors=True)
        async with _lock:
            _sessions.pop(session_id, None)
        raise HTTPException(
            status_code=422,
            detail=f"hash mismatch: expected {expected}, actual {actual_sha}",
        )

    _write_signal(state.filename, final_name, total, actual_sha)

    shutil.rmtree(session_dir, ignore_errors=True)
    async with _lock:
        _sessions.pop(session_id, None)

    return JSONResponse({
        "status": "success",
        "stored_filename": final_name,
        "size_bytes": total,
        "sha256": actual_sha,
    })


@app.post("/session/{session_id}/cancel")
async def session_cancel(session_id: str, request: Request):
    _require_token(request.headers.get("x-pairing-token"))

    async with _lock:
        state = _sessions.get(session_id)
        if state is not None:
            state.cancelled = True
            _sessions.pop(session_id, None)

    shutil.rmtree(SESSIONS_DIR / session_id, ignore_errors=True)
    return JSONResponse({"status": "cancelled"})


# ---------------------------------------------------------------------------
# Session cleanup loop
# ---------------------------------------------------------------------------

async def _cleanup_loop():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        async with _lock:
            stale = [
                sid for sid, s in _sessions.items()
                if (now - s.last_activity) > SESSION_TTL_SECONDS
            ]
            for sid in stale:
                _sessions.pop(sid, None)

        # Also delete orphan session dirs on disk
        try:
            for child in SESSIONS_DIR.iterdir():
                if child.is_dir() and (now - child.stat().st_mtime) > SESSION_TTL_SECONDS:
                    shutil.rmtree(child, ignore_errors=True)
        except Exception:
            pass


@app.on_event("startup")
async def _startup():
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.create_task(_cleanup_loop())