# desktop/ui/server_runner.py
"""
Runs the FastAPI server (receiver.server:app) in a background thread so
it doesn't block the PySide6 GUI event loop.

Starts Uvicorn, waits for it to bind, and exposes a stop() method that
sends the SHUTDOWN signal and joins the thread.
"""
import socket
import threading
import time
from typing import Optional

import uvicorn


def _wait_for_port(port: int, timeout_s: float = 10.0) -> bool:
    """Poll until port is listening or timeout expires."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(0.1)
    return False


class ServerRunner:
    """Manages the Uvicorn server thread."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8000):
        self.host = host
        self.port = port
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[uvicorn.Server] = None

    def start(self) -> bool:
        """Start Uvicorn in a background thread. Returns True if listening."""
        if self._thread is not None and self._thread.is_alive():
            return True

        config = uvicorn.Config(
            "receiver.server:app",
            host=self.host,
            port=self.port,
            log_level="info",
            access_log=True,
        )
        self._server = uvicorn.Server(config)

        def _run():
            try:
                self._server.run()
            except Exception as e:
                print(f"[ServerRunner] uvicorn crashed: {e}")

        self._thread = threading.Thread(target=_run, name="uvicorn", daemon=True)
        self._thread.start()

        ok = _wait_for_port(self.port, timeout_s=10.0)
        return ok

    def stop(self) -> None:
        """Signal uvicorn to shut down and wait for the thread."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._server = None
        self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()