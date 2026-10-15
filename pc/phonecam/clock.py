"""PING/PONG clock-offset estimation (docs/protocol.md §3, PING/PONG)."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


def now_us() -> int:
    """PC protocol clock: CLOCK_MONOTONIC in microseconds."""
    return time.monotonic_ns() // 1000


@dataclass(frozen=True, slots=True)
class ClockSample:
    rtt_us: int
    offset_us: int  # phone_clock - pc_clock


class ClockSync:
    """Keeps the last `window` samples; the offset comes from the one with the lowest RTT,
    which has the least queuing asymmetry between the two directions."""

    def __init__(self, window: int = 20) -> None:
        self._samples: deque[ClockSample] = deque(maxlen=window)
        self._lock = threading.Lock()
        self._last: ClockSample | None = None

    def add(self, t_pc_send_us: int, t_phone_us: int, t_pc_recv_us: int) -> ClockSample | None:
        rtt = t_pc_recv_us - t_pc_send_us
        if rtt < 0:
            return None
        sample = ClockSample(rtt, t_phone_us - (t_pc_send_us + t_pc_recv_us) // 2)
        with self._lock:
            self._samples.append(sample)
            self._last = sample
        return sample

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._last = None

    @property
    def best(self) -> ClockSample | None:
        with self._lock:
            return min(self._samples, key=lambda s: s.rtt_us, default=None)

    @property
    def last_rtt_us(self) -> int | None:
        with self._lock:
            return self._last.rtt_us if self._last else None

    @property
    def offset_us(self) -> int | None:
        best = self.best
        return best.offset_us if best else None

    def phone_to_pc(self, t_phone_us: int) -> int | None:
        off = self.offset_us
        return None if off is None else t_phone_us - off
