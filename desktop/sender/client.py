"""
SHAREX Windows Sender — Phase 2

Sends a file to the Android receiver at http://<phone-ip>:8080/upload.
Computes SHA-256 while streaming, verifies the server's returned hash.

Usage:
    python client.py --ip 192.168.137.238 --file "C:\\path\\to\\file.jpg"
    python client.py --ip 192.168.137.238 --file photo.jpg --token sharex-phase2-test
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def sha256_of_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def main() -> int:
    p = argparse.ArgumentParser(description="SHAREX Windows Sender (Phase 2)")
    p.add_argument("--ip", required=True, help="Phone's IP on the hotspot (e.g. 192.168.137.238)")
    p.add_argument("--port", type=int, default=8080, help="Phone receiver port (default 8080)")
    p.add_argument("--file", required=True, help="Path to file to send")
    p.add_argument("--token", default="sharex-phase2-test", help="Pairing token")
    args = p.parse_args()

    file_path = Path(args.file).expanduser().resolve()
    if not file_path.is_file():
        print(f"ERROR: file not found: {file_path}", file=sys.stderr)
        return 1

    size = file_path.stat().st_size
    print(f"File:     {file_path}")
    print(f"Size:     {human_bytes(size)} ({size} bytes)")
    print(f"Target:   http://{args.ip}:{args.port}/upload")

    print("Computing SHA-256 of source...")
    t0 = time.time()
    local_sha = sha256_of_file(file_path)
    print(f"Local SHA-256: {local_sha}  ({(time.time()-t0):.2f}s)")

    # Streaming multipart with progress
    def gen():
        with file_path.open("rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                yield b



    # Use MultipartEncoderMonitor for a live progress bar
    def make_encoder():
        # Real file object — MultipartEncoder computes Content-Length from it
        f = file_path.open("rb")
        return MultipartEncoder(
            fields={
                "file": (file_path.name, f, "application/octet-stream"),
            }
        ), f

    encoder, _fobj = make_encoder()
    last_report = [0.0]
    start_time = [time.time()]

    def callback(mon):
        now = time.time()
        if now - last_report[0] < 0.1:
            return
        last_report[0] = now
        pct = (mon.bytes_read / mon.len * 100) if mon.len else 0
        elapsed = now - start_time[0]
        speed = mon.bytes_read / elapsed if elapsed > 0 else 0
        bar_len = 30
        filled = int(bar_len * pct / 100)
        bar = "#" * filled + "-" * (bar_len - filled)
        sys.stdout.write(
            f"\r[{bar}] {pct:5.1f}%  "
            f"{human_bytes(mon.bytes_read)}/{human_bytes(mon.len)}  "
            f"{human_bytes(speed)}/s"
        )
        sys.stdout.flush()

    monitor = MultipartEncoderMonitor(encoder, callback)

    headers = {
        "X-Pairing-Token": args.token,
        "X-Filename": file_path.name,
        "Content-Type": monitor.content_type,
    }

    print("Uploading...")
    try:
        r = requests.post(
            f"http://{args.ip}:{args.port}/upload",
            data=monitor,
            headers=headers,
            timeout=600,
        )
    except requests.exceptions.ConnectionError as e:
        print(f"\nERROR: connection failed: {e}", file=sys.stderr)
        print("  Check: phone on the same hotspot network? receiver running on the phone?", file=sys.stderr)
        return 2
    except requests.exceptions.Timeout:
        print("\nERROR: timeout — phone didn't respond within 10 minutes", file=sys.stderr)
        return 3
    finally:
        sys.stdout.write("\n")
        try:
            _fobj.close()
        except Exception:
            pass

    print(f"HTTP {r.status_code}")
    print("Response body:")
    print(r.text)

    if r.status_code != 200:
        print("Server did not return 200 — transfer failed.", file=sys.stderr)
        return 4

    try:
        payload = r.json()
    except Exception:
        print("WARNING: response is not JSON", file=sys.stderr)
        return 5

    remote_sha = payload.get("sha256", "")
    remote_size = payload.get("size_bytes", -1)

    print()
    print("=" * 60)
    print(f"Local  SHA-256: {local_sha}")
    print(f"Remote SHA-256: {remote_sha}")
    print(f"Local  size:    {size} bytes")
    print(f"Remote size:    {remote_size} bytes")
    print("=" * 60)

    ok_hash = (local_sha == remote_sha)
    ok_size = (size == remote_size)

    if ok_hash and ok_size:
        print("✅ SHA-256 MATCH — file transferred intact")
        try:
            out = Path(__file__).parent / "last_response.json"
            out.write_text(json.dumps(payload, indent=2))
            print(f"Saved response to: {out}")
        except Exception:
            pass
        return 0
    else:
        if not ok_hash:
            print("❌ SHA-256 MISMATCH — file corrupted in transit")
        if not ok_size:
            print("❌ size mismatch — some bytes missing")
        return 6


if __name__ == "__main__":
    sys.exit(main())