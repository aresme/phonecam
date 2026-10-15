from phonecam.adb import parse_devices

OUTPUT = """\
* daemon not running; starting now at tcp:5037
* daemon started successfully
List of devices attached
R58M41ABCDE       device usb:1-1 product:X6886-OP model:Infinix_X6886 device:Infinix-X6886 transport_id:1
192.168.1.20:5555      device product:foo model:Pixel_7 device:panther transport_id:3
ZY22ABCDEF             unauthorized usb:1-2 transport_id:4

"""


def test_parse_devices() -> None:
    devs = parse_devices(OUTPUT)
    assert [d.serial for d in devs] == ["R58M41ABCDE", "192.168.1.20:5555", "ZY22ABCDEF"]
    usb, net, unauth = devs
    assert usb.ready and not usb.is_network and usb.model == "Infinix_X6886"
    assert usb.label.startswith("Infinix X6886 · USB")
    assert net.is_network and "Wi-Fi adb" in net.label
    assert not unauth.ready and "(unauthorized)" in unauth.label


def test_parse_empty() -> None:
    assert parse_devices("List of devices attached\n\n") == []
