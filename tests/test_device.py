import pytest

from educelab.devices import Device, DeviceInfo, synchronized
from educelab.devices.errors import NotConnected


class Recorder(Device):
    def __init__(self, fail_open: bool = False, fail_close: bool = False):
        super().__init__()
        self.calls: list[str] = []
        self.fail_open = fail_open
        self.fail_close = fail_close

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="test", model="Recorder")

    def _open(self) -> None:
        self.calls.append("open")
        if self.fail_open:
            raise OSError("port missing")

    def _close(self) -> None:
        self.calls.append("close")
        if self.fail_close:
            raise OSError("port vanished")

    @synchronized
    def ping(self) -> str:
        self.calls.append("ping")
        return "pong"


def test_constructor_does_no_io():
    device = Recorder()
    assert device.calls == []
    assert not device.is_open


def test_open_is_idempotent():
    device = Recorder()
    device.open()
    device.open()
    assert device.is_open
    assert device.calls == ["open"]


def test_close_is_idempotent():
    device = Recorder()
    device.open()
    device.close()
    device.close()
    assert not device.is_open
    assert device.calls == ["open", "close"]


def test_close_without_open_does_nothing():
    device = Recorder()
    device.close()
    assert device.calls == []


def test_device_can_reopen_after_close():
    device = Recorder()
    device.open()
    device.close()
    device.open()
    assert device.is_open
    assert device.calls == ["open", "close", "open"]


def test_failed_open_leaves_device_closed():
    device = Recorder(fail_open=True)
    with pytest.raises(OSError):
        device.open()
    assert not device.is_open


def test_failed_close_still_marks_device_closed():
    device = Recorder(fail_close=True)
    device.open()
    with pytest.raises(OSError):
        device.close()
    assert not device.is_open


def test_context_manager_opens_and_closes():
    with Recorder() as device:
        assert device.is_open
    assert not device.is_open
    assert device.calls == ["open", "close"]


def test_context_manager_closes_on_error():
    device = Recorder()
    with pytest.raises(RuntimeError), device:
        raise RuntimeError("boom")
    assert not device.is_open


def test_synchronized_method_raises_not_connected_when_closed():
    device = Recorder()
    with pytest.raises(NotConnected, match="Recorder"):
        device.ping()
    assert device.calls == []


def test_synchronized_method_runs_when_open():
    with Recorder() as device:
        assert device.ping() == "pong"


def test_device_info_is_frozen():
    info = DeviceInfo(backend="test", model="Recorder", address="/dev/ttyUSB0")
    with pytest.raises(AttributeError):
        info.model = "Other"  # type: ignore[misc]


def test_discover_defaults_to_empty_list():
    assert Recorder.discover() == []
