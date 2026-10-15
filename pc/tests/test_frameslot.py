import threading
import time

from phonecam.frameslot import LatestFrameSlot


def test_overwrite_keeps_latest_only() -> None:
    slot: LatestFrameSlot[int] = LatestFrameSlot()
    slot.put(1)
    slot.put(2)
    slot.put(3)
    assert slot.get(0) == 3
    assert slot.overwritten == 2
    assert slot.get(0.01) is None  # nothing new


def test_get_blocks_until_put() -> None:
    slot: LatestFrameSlot[str] = LatestFrameSlot()
    threading.Timer(0.05, slot.put, args=("x",)).start()
    t = time.monotonic()
    assert slot.get(1) == "x"
    assert time.monotonic() - t >= 0.04


def test_close_unblocks_and_reopen_discards() -> None:
    slot: LatestFrameSlot[int] = LatestFrameSlot()
    threading.Timer(0.05, slot.close).start()
    assert slot.get(1) is None
    slot.put(7)
    slot.reopen()
    assert slot.get(0.01) is None
    slot.put(8)
    assert slot.get(0) == 8


def test_fast_producer_slow_consumer_never_backlogs() -> None:
    slot: LatestFrameSlot[int] = LatestFrameSlot()
    got: list[int] = []
    stop = threading.Event()

    def consume() -> None:
        while not stop.is_set():
            v = slot.get(0.1)
            if v is not None:
                got.append(v)
                time.sleep(0.01)

    t = threading.Thread(target=consume)
    t.start()
    for i in range(500):
        slot.put(i)
        time.sleep(0.0005)
    time.sleep(0.05)
    stop.set()
    t.join()
    assert got == sorted(got) and len(set(got)) == len(got)  # in order, each at most once
    assert got[-1] == 499  # the consumer ends on the newest frame
    assert len(got) < 200
