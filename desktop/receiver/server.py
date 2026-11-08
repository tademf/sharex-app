# desktop/receiver/server.py
import hashlib
import uuid
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.responses import JSONResponse

app = FastAPI(title="SHAREX Receiver", version="0.1.0")

DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

PAIRING_TOKEN = "sharex-phase1-test"
CHUNK_SIZE = 1024 * 1024  # 1 MB


def sanitize_filename(filename: str | None) -> str:
    if not filename:
        return f"{uuid.uuid4().hex}.bin"
    safe = Path(filename).name
    for ch in ':*?"<>|':
        safe = safe.replace(ch, "_")
    return f"{uuid.uuid4().hex}_{safe}"


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    x_pairing_token: str = Header(...),
):
    if x_pairing_token != PAIRING_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid pairing token")

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


@app.get("/health")
async def health():
    return {"status": "alive", "service": "SHAREX Receiver"}