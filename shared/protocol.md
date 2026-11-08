# SHAREX Transfer Protocol v2

**Introduced:** Phase 5
**Supersedes:** v1 (Phase 1-4 single-POST transfer)

## Overview

Version 2 introduces session-based chunked transfer with pause, resume,
and cancel. It coexists with v1 (`POST /upload`) for backward compatibility
during the transition, then replaces it.

All endpoints use HTTP/1.1 on the local network. The `X-Pairing-Token` header
authenticates every request. Chunks are raw bytes, not multipart — simpler,
faster, streamable.

## Session lifecycle

### 1. Start

    POST /session/start
    Headers:
        X-Pairing-Token: <token>
        Content-Type: application/json
    Body:
        {
          "filename": "photo.jpg",
          "total_size": 2147483648,
          "sha256": "<64-hex-whole-file-hash>",
          "chunk_size": 4194304,
          "resume_id": null | "<existing-session-uuid>"
        }

    Response 200:
        {
          "session_id": "<uuid>",
          "chunks_needed": [0, 1, 2, ...],
          "chunks_received": [0, 1, 2, ...],  // present only when resuming
          "resume_token": "<hex>",             // for this session
          "expires_at": <unix-timestamp>
        }

`resume_id` is the previous `session_id` to resume. If null, a new session
is created. If provided but the session is missing or expired, the server
creates a new one and returns a full `chunks_needed` list.

### 2. Send chunk

    POST /session/<session_id>/chunk/<index>
    Headers:
        X-Pairing-Token: <token>
    Body: raw bytes of chunk `index` (length = chunk_size or smaller for last)

    Response 200:
        {
          "index": <int>,
          "bytes_written": <int>,
          "chunks_received": <int>,
          "chunks_total": <int>
        }

Chunks can arrive in any order. Re-sending a chunk that was already received
is a no-op (idempotent). This makes retries safe.

### 3. Finish

    POST /session/<session_id>/finish
    Headers:
        X-Pairing-Token: <token>
    Body:
        {"sha256": "<64-hex-whole-file-hash>"}

    Response 200:
        {
          "status": "success",
          "stored_filename": "<uuid>_<original>",
          "size_bytes": <int>,
          "sha256": "<64-hex>"
        }

The server reads all chunks in order, verifies the whole-file SHA-256,
and moves the assembled file to the downloads directory. If the hash
doesn't match, returns 422 with `{"status":"error","detail":"hash mismatch"}`.

### 4. Cancel

    POST /session/<session_id>/cancel
    Headers:
        X-Pairing-Token: <token>

    Response 200: {"status": "cancelled"}

Chunks are deleted. The session is removed.

## Storage layout

Each session gets its own directory under the app's cache:

    /data/data/com.sharex.app/cache/sessions/<session_id>/
        meta.json              # filename, total_size, chunk_size, sha256_expected
        chunk_00000            # raw bytes
        chunk_00001
        ...

On `/finish`, chunks are concatenated in order to a temp file, SHA-256
verified, then moved to:

    /storage/emulated/0/Android/data/com.sharex.app/files/Download/SHAREX/<uuid>_<filename>

## Expiry

Sessions expire 1 hour after the last activity (chunk received or finish
attempt). A background cleanup task runs every 5 minutes, deleting expired
session directories. The sender receives `expires_at` in the start response
and should finish or resume before it expires.

## Compatibility

- v1 `POST /upload` remains for desktop `client.py` compatibility (Phase 2 scripts).
- v2 endpoints require `X-Pairing-Token`; v1 accepts it too but the token
  scheme is unified in Phase 5.4.

## Why this design

- **Resume works because chunk state is on disk.** A crash mid-transfer
  leaves the received chunks intact. `resume_id` retrieves them.
- **Chunks are idempotent.** Re-sending chunk 47 doesn't corrupt anything.
  Retries are safe without knowing whether the previous attempt landed.
- **SHA-256 at finish, not at every chunk.** Chunk-level hashing would be
  8x the CPU for 4 MB chunks. Whole-file at the end is cheaper and catches
  the same corruption.
- **Session IDs isolate concurrent transfers.** Three files uploading at
  once don't interfere.
