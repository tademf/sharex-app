# desktop/receiver/token_manager.py
"""
Short-lived pairing token manager.

Generates random tokens for QR pairing, tracks them in memory, and
expires them after a configurable TTL. Once a token is used successfully,
it's invalidated (single-use).

In-memory only — tokens do not persist across server restarts.
"""
import secrets
import threading
import time
from typing import Optional


class TokenManager:
    def __init__(self, ttl_seconds: int = 300):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        # token string -> expiry unix timestamp
        self._active: dict[str, float] = {}

    def issue(self) -> tuple[str, float]:
        """Generate a fresh token. Returns (token, expires_at_unix)."""
        token = secrets.token_hex(8)  # 16 hex chars
        expires = time.time() + self._ttl
        with self._lock:
            self._active[token] = expires
        return token, expires

    def validate_and_consume(self, token: str) -> bool:
        """
        Returns True if token is valid (exists and not expired).
        On success, the token is removed (single-use).
        On expiry, the token is also removed.
        """
        with self._lock:
            expires = self._active.get(token)
            if expires is None:
                return False
            if time.time() > expires:
                self._active.pop(token, None)
                return False
            # Valid — remove for single-use semantics
            self._active.pop(token, None)
            return True

    def peek(self, token: str) -> Optional[float]:
        """Non-consuming check, for diagnostics. Returns expiry or None."""
        with self._lock:
            expires = self._active.get(token)
            if expires is None or time.time() > expires:
                return None
            return expires

    def cleanup_expired(self) -> int:
        """Remove expired tokens. Returns count removed. Called periodically."""
        now = time.time()
        with self._lock:
            dead = [t for t, exp in self._active.items() if now > exp]
            for t in dead:
                self._active.pop(t, None)
            return len(dead)

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)