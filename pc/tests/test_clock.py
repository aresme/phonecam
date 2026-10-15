from phonecam.clock import ClockSync


def test_symmetric_path_gives_exact_offset() -> None:
    c = ClockSync()
    # PC sends at 1000, phone (clock +5000) answers at its 6050, PC receives at 1100.
    s = c.add(1000, 6050, 1100)
    assert s is not None
    assert s.rtt_us == 100
    assert c.offset_us == 5000
    assert c.phone_to_pc(9000) == 4000


def test_lowest_rtt_sample_wins() -> None:
    c = ClockSync(window=5)
    c.add(0, 5000 + 400, 1000)  # rtt 1000, asymmetric -> offset estimate off by 100
    c.add(2000, 5000 + 2010, 2020)  # rtt 20 -> good estimate (5000)
    c.add(3000, 5000 + 3700, 4000)
    assert c.best is not None and c.best.rtt_us == 20
    assert c.offset_us == 5000
    assert c.last_rtt_us == 1000


def test_window_forgets_old_samples() -> None:
    c = ClockSync(window=2)
    c.add(0, 10, 10)  # rtt 10, offset 5
    c.add(100, 200, 200)  # rtt 100
    c.add(300, 400, 400)  # rtt 100; first sample evicted
    assert c.best is not None and c.best.rtt_us == 100


def test_negative_rtt_ignored_and_empty_state() -> None:
    c = ClockSync()
    assert c.add(100, 0, 50) is None
    assert c.offset_us is None
    assert c.phone_to_pc(1) is None
