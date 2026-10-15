from phonecam.latency_test import FlashDetector


def feed(
    det: FlashDetector, t0: int, n: int, luma: float, step_us: int = 33_333
) -> tuple[int, float | None]:
    found = None
    t = t0
    for _ in range(n):
        r = det.sample(t, luma)
        found = found if found is not None else r
        t += step_us
    return t, found


def test_detects_flip_delay() -> None:
    det = FlashDetector()
    t, _ = feed(det, 0, 30, 20.0)  # dark
    t, _ = feed(det, t, 30, 200.0)  # bright, establishes contrast
    t, _ = feed(det, t, 30, 20.0)
    det.flip(t, to_white=True)
    t2, _ = feed(det, t, 3, 20.0)  # ~100 ms still dark (latency)
    _, found = feed(det, t2, 5, 210.0)
    assert found is not None and 95 < found < 105
    assert det.results == [found]


def test_no_result_without_contrast() -> None:
    det = FlashDetector()
    t, _ = feed(det, 0, 60, 100.0)
    det.flip(t, to_white=True)
    _, found = feed(det, t, 10, 105.0)
    assert found is None and not det.contrast_ok


def test_ignores_too_late() -> None:
    det = FlashDetector(max_delay_ms=200)
    t, _ = feed(det, 0, 30, 20.0)
    t, _ = feed(det, t, 30, 200.0)
    t, _ = feed(det, t, 30, 20.0)
    det.flip(t, to_white=True)
    t, _ = feed(det, t, 10, 20.0)  # 330 ms: give up
    _, found = feed(det, t, 5, 200.0)
    assert found is None


def test_audio_onset_detector() -> None:
    from phonecam.latency_test import AudioOnsetDetector

    det = AudioOnsetDetector()
    t = 0
    for _ in range(50):
        det.sample(t, 100.0)
        t += 10_000
    det.beep(t)
    for _ in range(8):  # 80 ms of quiet after the beep was played
        assert det.sample(t, 120.0) is None
        t += 10_000
    d = det.sample(t, 5000.0)
    assert d is not None and 75 < d < 85
