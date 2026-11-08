# desktop/database/__init__.py
"""
SQLite setup for SHAREX transfer history.
Database file lives at desktop/sharx_history.db.

Phase 5 adds three columns to `transfers`:
  session_id      — v2 session UUID (NULL for v1/legacy)
  resume_id       — session to resume from (NULL for fresh)
  chunks_json     — JSON list of received chunk indices (for resume)
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "sharx_history.db"


def _migrate(conn: sqlite3.Connection) -> None:
    """Add Phase 5 columns if they don't exist. Idempotent."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(transfers)")}
    for col, decl in [
        ("session_id", "TEXT"),
        ("resume_id", "TEXT"),
        ("chunks_json", "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE transfers ADD COLUMN {col} {decl}")
    conn.commit()


def init_db() -> None:
    """Create tables if they don't exist. Idempotent.

    Order matters for existing (pre-Phase 5) databases:
      1. CREATE TABLE IF NOT EXISTS  — no-op on old DBs (does NOT add columns)
      2. _migrate()                  — ALTER TABLE adds session_id/resume_id/chunks_json
      3. CREATE INDEX                — safe now that the columns exist
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL,           -- 'sent' or 'received'
                filename TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT,
                device_name TEXT,
                peer_ip TEXT,
                status TEXT NOT NULL,              -- 'success', 'failed', 'cancelled', 'paused'
                error TEXT,
                created_at TEXT NOT NULL,          -- ISO8601
                session_id TEXT,
                resume_id TEXT,
                chunks_json TEXT
            )
        """)
        conn.commit()
        # Migrate BEFORE creating indexes on the new columns.
        _migrate(conn)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_transfers_created
            ON transfers(created_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_transfers_session
            ON transfers(session_id)
        """)
        conn.commit()


def record_transfer(
    direction: str,
    filename: str,
    size_bytes: int,
    sha256: Optional[str] = None,
    device_name: Optional[str] = None,
    peer_ip: Optional[str] = None,
    status: str = "success",
    error: Optional[str] = None,
    session_id: Optional[str] = None,
    resume_id: Optional[str] = None,
    chunks_received: Optional[list[int]] = None,
) -> int:
    """Insert a transfer record. Returns the new row ID."""
    chunks_json = json.dumps(chunks_received) if chunks_received is not None else None
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            INSERT INTO transfers
            (direction, filename, size_bytes, sha256, device_name,
             peer_ip, status, error, created_at,
             session_id, resume_id, chunks_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            direction, filename, size_bytes, sha256, device_name,
            peer_ip, status, error, datetime.now().isoformat(timespec="seconds"),
            session_id, resume_id, chunks_json,
        ))
        conn.commit()
        return cur.lastrowid


def update_transfer_session(
    row_id: int,
    session_id: Optional[str] = None,
    chunks_received: Optional[list[int]] = None,
    status: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Update a transfer row's session state (for pause/resume tracking)."""
    sets = []
    args = []
    if session_id is not None:
        sets.append("session_id = ?")
        args.append(session_id)
    if chunks_received is not None:
        sets.append("chunks_json = ?")
        args.append(json.dumps(chunks_received))
    if status is not None:
        sets.append("status = ?")
        args.append(status)
    if error is not None:
        sets.append("error = ?")
        args.append(error)
    if not sets:
        return
    args.append(row_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"UPDATE transfers SET {', '.join(sets)} WHERE id = ?",
            args,
        )
        conn.commit()


def recent_transfers(limit: int = 50) -> list[dict]:
    """Return the last N transfers, newest first."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM transfers
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def resumable_transfers() -> list[dict]:
    """
    Return transfers that can be resumed:
    status = 'paused' OR status = 'failed' with session_id present.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM transfers
            WHERE direction = 'sent'
              AND status IN ('paused', 'failed')
              AND session_id IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 20
        """).fetchall()
        return [dict(r) for r in rows]


# Initialize on import
init_db()