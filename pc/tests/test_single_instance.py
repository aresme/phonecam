import threading
from pathlib import Path

from phonecam.single_instance import SingleInstance


def test_second_instance_activates_first(tmp_path: Path) -> None:
    path = tmp_path / "pc.sock"
    shown = threading.Event()
    first = SingleInstance(path)
    assert first.acquire(shown.set)
    second = SingleInstance(path)
    assert not second.acquire(lambda: None)
    assert shown.wait(2)
    first.release()
    assert not path.exists()


def test_stale_socket_is_replaced(tmp_path: Path) -> None:
    import socket

    path = tmp_path / "pc.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # file left behind, nobody listening (crashed instance)
    inst = SingleInstance(path)
    assert inst.acquire(lambda: None)
    inst.release()
