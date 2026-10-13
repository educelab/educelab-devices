import pytest

from educelab.devices import DeviceInfo
from educelab.devices.errors import NotConnected
from educelab.devices.hubs import Hub


class StubHub(Hub):
    power_cycle_off_time = 0.0

    def __init__(self) -> None:
        super().__init__()
        self.powered = {0: True, 1: True}
        self.writes: list[tuple[int, bool]] = []

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="test", model="StubHub")

    @property
    def ports(self) -> tuple[int, ...]:
        return (0, 1)

    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _set_power(self, port: int, on: bool) -> None:
        self.writes.append((port, on))
        self.powered[port] = on

    def _is_powered(self, port: int) -> bool:
        return self.powered[port]


@pytest.fixture
def hub():
    with StubHub() as device:
        yield device


def test_power_off_and_on(hub):
    hub.power_off(1)
    assert not hub.is_powered(1)
    hub.power_on(1)
    assert hub.is_powered(1)


def test_power_cycle_turns_port_off_then_on(hub):
    hub.power_cycle(0)
    assert hub.writes == [(0, False), (0, True)]


def test_power_cycle_rejects_negative_off_time(hub):
    with pytest.raises(ValueError):
        hub.power_cycle(0, off_time=-1)
    assert hub.writes == []


@pytest.mark.parametrize("port", [2, -1, True, 1.0])
def test_unknown_port_raises_key_error(hub, port):
    with pytest.raises(KeyError):
        hub.power_on(port)


def test_hub_methods_need_open_device():
    with pytest.raises(NotConnected):
        StubHub().is_powered(0)


@pytest.mark.parametrize("off_time", [float("nan"), float("inf")])
def test_power_cycle_rejects_non_finite_off_time(hub, off_time):
    with pytest.raises(ValueError):
        hub.power_cycle(0, off_time=off_time)
    assert hub.writes == []


def test_power_cycle_restores_power_if_interrupted(hub, monkeypatch):
    def interrupt(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        hub.power_cycle(0)
    assert hub.is_powered(0)
