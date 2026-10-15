"""One PhoneCam per user session: two instances would fight over /dev/video10 and PhoneCam Mic.

The first instance listens on a Unix socket in $XDG_RUNTIME_DIR. A second launch connects,
asks the first to show its window, and exits. A stale socket (crashed instance) is detected by
the refused connection and replaced.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


def default_path() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/phonecam-{os.getuid()}")) / "phonecam.sock"


class SingleInstance:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()
        self._server: socket.socket | None = None
        self._on_activate: Callable[[], None] = lambda: None

    def acquire(self, on_activate: Callable[[], None]) -> bool:
        """True if we are the only instance (and now listen); False if another one is running
        (it has been asked to show its window)."""
        self._on_activate = on_activate
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(str(self.path))
                s.sendall(b"show\n")
            return False
        except (TimeoutError, FileNotFoundError, ConnectionRefusedError):
            pass  # nobody listening: we're first (or the socket is stale)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.path))
        os.chmod(self.path, 0o600)
        srv.listen(4)
        self._server = srv
        threading.Thread(target=self._serve, name="single-instance", daemon=True).start()
        return True

    def _serve(self) -> None:
        assert self._server is not None
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(1)
                with contextlib.suppress(OSError):
                    if conn.recv(16).startswith(b"show"):
                        log.info("another launch asked us to show the window")
                        self._on_activate()

    def release(self) -> None:
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            self._server = None
