# desktop/database/__init__.py
"""
SQLite setup for SHAREX transfer history.
Database file lives at desktop/sharx_history.db.
"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "sharx_history.db"


def init_db() -> None:
    """Create tables if they don't exist. Idempotent."""
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
                status TEXT NOT NULL,              -- 'success', 'failed', 'cancelled'
                error TEXT,
                created_at TEXT NOT NULL           -- ISO8601
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_transfers_created
            ON transfers(created_at DESC)
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
) -> int:
    """Insert a transfer record. Returns the new row ID."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            INSERT INTO transfers
            (direction, filename, size_bytes, sha256, device_name,
             peer_ip, status, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            direction, filename, size_bytes, sha256, device_name,
            peer_ip, status, error, datetime.now().isoformat(timespec="seconds")
        ))
        conn.commit()
        return cur.lastrowid


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


# Initialize on import
init_db()