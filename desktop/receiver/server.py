# desktop/receiver/server.py
import hashlib
import io
import json
import socket
import uuid
from pathlib import Path

import qrcode
from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.responses import JSONResponse, Response

from receiver.token_manager import TokenManager

app = FastAPI(title="SHAREX Receiver", version="0.3.0")

DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Pairing token lifetime and manager
PAIRING_TOKEN_TTL = 300  # 5 minutes
tokens = TokenManager(ttl_seconds=PAIRING_TOKEN_TTL)

CHUNK_SIZE = 1024 * 1024  # 1 MB
TRANSFER_PORT = 8000


def sanitize_filename(filename: str | None) -> str:
    if not filename:
        return f"{uuid.uuid4().hex}.bin"
    safe = Path(filename).name
    for ch in ':*?"<>|':
        safe = safe.replace(ch, "_")
    return f"{uuid.uuid4().hex}_{safe}"


def local_ip_on_hotspot() -> str:
    """
    Return the local IP on the hotspot network.
    Uses a UDP trick: connecting a UDP socket to a remote address
    doesn't send packets but reveals which local interface would be used.
    Falls back to 127.0.0.1 if no route exists.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        # 192.168.137.0/24 is the default Windows hotspot subnet.
        # Using its gateway here just picks the right interface.
        s.connect(("192.168.137.1", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.get("/health")
async def health():
    return {"status": "alive", "service": "SHAREX Receiver"}


@app.get("/pairing")
async def issue_pairing():
    """
    Issue a fresh pairing token. Returns the token, expiry, and
    connection info suitable for embedding in a QR code.

    Side effect: any previously-issued-but-unused tokens for this
    server remain valid until their TTL expires. Only tokens consumed
    on successful upload are invalidated. This allows regenerating
    a QR without invalidating a scan in progress.
    """
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
    """
    Issue a fresh pairing token and render it as a QR code PNG.
    Payload is the same JSON as /pairing.
    """
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


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    x_pairing_token: str = Header(...),
):
    # Validate token. Single-use: consumed on success.
    # Note: we consume even if the upload later fails to write,
    # because the token has already been presented. If the upload
    # fails, the client must rescan.
    if not tokens.validate_and_consume(x_pairing_token):
        raise HTTPException(status_code=403, detail="Invalid or expired pairing token")

    final_name = sanitize_filename(file.filename)
    dest_path = DOWNLOAD_DIR / final_name

    sha256 = hashlib.sha256()
    total_bytes = 0

    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                sha256.update(chunk)
                out.write(chunk)
                total_bytes += len(chunk)
        await file.close()
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


@app.get("/pairing/status")
async def pairing_status():
    """Diagnostic endpoint — shows how many tokens are currently active."""
    return {"active_tokens": tokens.active_count(), "ttl_seconds": PAIRING_TOKEN_TTL}