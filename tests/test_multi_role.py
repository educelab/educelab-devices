"""A device can offer several roles, e.g. a turntable with a built-in light."""

from collections.abc import Mapping

from educelab.devices import DeviceInfo
from educelab.devices.lights import RGB, Channel, Light
from educelab.devices.stages import Axis, AxisKind, Stage


class Turntable(Stage, Light):
    can_home = True
    requires_homing = True

    def __init__(self) -> None:
        super().__init__()
        self.angle = 0.0
        self.halo_level = 0.0

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="test", model="Turntable")

    @property
    def axes(self) -> tuple[Axis, ...]:
        return (Axis("theta", AxisKind.ROTARY, wraps=True),)

    @property
    def channels(self) -> tuple[Channel, ...]:
        return (Channel("halo"),)

    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _move_to(self, targets: Mapping[str, float], timeout: float) -> None:
        self.angle = targets["theta"]

    def _jog(self, axis: str, direction: int) -> None:
        pass

    def _stop(self) -> None:
        pass

    def _position(self) -> Mapping[str, float]:
        return {"theta": self.angle}

    def _home(self, timeout: float) -> None:
        self.angle = 0.0

    def _set_level(self, channel: Channel, level: float) -> None:
        self.halo_level = level

    def _set_color(self, channel: Channel, color: RGB) -> None:
        raise AssertionError("no RGB channels")


def test_device_offers_both_roles():
    with Turntable() as table:
        table.home()
        table.move_to({"theta": 90.0})
        table.set_level("halo", 0.5)
        assert table.position() == {"theta": 90.0}
        assert table.halo_level == 0.5


def test_close_resets_every_role_state():
    table = Turntable()
    with table:
        table.home()
    assert not table.is_homed
