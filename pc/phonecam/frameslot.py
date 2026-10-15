"""Latest-frame-only hand-off between threads (docs/architecture.md, 'Latest-frame slot')."""

from __future__ import annotations

import threading


class LatestFrameSlot[T]:
    """One-element mailbox: put() overwrites, get() returns each item at most once.

    The producer never blocks and the consumer never sees stale frames, so no backlog can
    build up between two pipeline stages running at different speeds.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._item: T | None = None
        self._seq = 0
        self._taken = 0
        self._closed = False
        self.overwritten = 0

    def put(self, item: T) -> None:
        with self._cond:
            if self._seq != self._taken:
                self.overwritten += 1
            self._item = item
            self._seq += 1
            self._cond.notify_all()

    def get(self, timeout: float | None = None) -> T | None:
        """Wait for an item newer than the last one returned. None on timeout or close."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq != self._taken or self._closed, timeout):
                return None
            if self._closed and self._seq == self._taken:
                return None
            self._taken = self._seq
            item, self._item = self._item, None
            return item

    def peek(self) -> T | None:
        with self._cond:
            return self._item

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def reopen(self) -> None:
        with self._cond:
            self._closed = False
            self._item = None
            self._taken = self._seq
