import threading
from collections.abc import Mapping

import pytest

from educelab.devices import DeviceInfo
from educelab.devices.errors import NotConnected, PositionError
from educelab.devices.stages import Axis, AxisKind, Stage


class StubStage(Stage):
    def __init__(self, requires_homing: bool = False) -> None:
        super().__init__()
        self.requires_homing = requires_homing
        self.pos = {"x": 0.0, "theta": 0.0}
        self.calls: list[tuple] = []

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(backend="test", model="StubStage")

    @property
    def axes(self) -> tuple[Axis, ...]:
        return (
            Axis("x", AxisKind.LINEAR, 0.0, 100.0),
            Axis("theta", AxisKind.ROTARY, wraps=True),
        )

    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _move_to(self, targets: Mapping[str, float], timeout: float) -> None:
        self.calls.append(("move_to", dict(targets), timeout))
        self.pos.update(targets)

    def _jog(self, axis: str, direction: int) -> None:
        self.calls.append(("jog", axis, direction))

    def _stop(self) -> None:
        self.calls.append(("stop",))

    def _position(self) -> Mapping[str, float]:
        return self.pos

    def _home(self, timeout: float) -> None:
        self.calls.append(("home", timeout))


@pytest.fixture
def stage():
    with StubStage() as device:
        yield device


def test_axis_units():
    assert Axis("x", AxisKind.LINEAR).unit == "mm"
    assert Axis("r", AxisKind.ROTARY).unit == "deg"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": AxisKind.LINEAR, "wraps": True},
        {"kind": AxisKind.ROTARY, "wraps": True, "maximum": 360.0},
        {"kind": AxisKind.LINEAR, "minimum": 10.0, "maximum": 0.0},
    ],
)
def test_invalid_axis_raises(kwargs):
    with pytest.raises(ValueError):
        Axis("a", **kwargs)


def test_move_to_passes_default_timeout(stage):
    stage.move_to({"x": 50})
    assert stage.calls == [("move_to", {"x": 50.0}, Stage.move_timeout)]


def test_move_to_passes_explicit_timeout(stage):
    stage.move_to({"x": 50}, timeout=2.5)
    assert stage.calls[-1][2] == 2.5


@pytest.mark.parametrize("timeout", [0, -1])
def test_non_positive_timeout_raises(stage, timeout):
    with pytest.raises(ValueError):
        stage.move_to({"x": 50}, timeout=timeout)


@pytest.mark.parametrize("target", [-0.1, 100.1])
def test_move_outside_limits_raises(stage, target):
    with pytest.raises(PositionError):
        stage.move_to({"x": target})
    assert stage.calls == []


def test_move_to_wrapping_axis_reduces_target(stage):
    stage.move_to({"theta": 370.0})
    assert stage.pos["theta"] == pytest.approx(10.0)
    stage.move_to({"theta": -90.0})
    assert stage.pos["theta"] == pytest.approx(270.0)


def test_unknown_axis_raises_key_error(stage):
    with pytest.raises(KeyError, match="z"):
        stage.move_to({"z": 1.0})


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_non_finite_target_raises(stage, value):
    with pytest.raises(ValueError):
        stage.move_to({"x": value})


def test_empty_move_does_nothing(stage):
    stage.move_to({})
    stage.move_by({})
    assert stage.calls == []


def test_move_by_checks_resulting_position(stage):
    stage.move_to({"x": 90.0})
    with pytest.raises(PositionError):
        stage.move_by({"x": 20.0})
    stage.move_by({"x": -40.0})
    assert stage.pos["x"] == 50.0


def test_move_by_wraps_rotary_axis(stage):
    stage.move_to({"theta": 350.0})
    stage.move_by({"theta": 20.0})
    assert stage.pos["theta"] == pytest.approx(10.0)


@pytest.mark.parametrize("direction", [0, 2, True])
def test_jog_rejects_bad_direction(stage, direction):
    with pytest.raises(ValueError):
        stage.jog("x", direction)


def test_jog_starts_motion(stage):
    stage.jog("x", -1)
    assert stage.calls == [("jog", "x", -1)]


def test_home_on_stage_without_reference_raises(stage):
    with pytest.raises(NotImplementedError):
        stage.home()


def test_stage_requiring_homing_refuses_to_move_until_homed():
    with StubStage(requires_homing=True) as stage:
        assert not stage.is_homed
        with pytest.raises(PositionError, match="home"):
            stage.move_to({"x": 1.0})
        with pytest.raises(PositionError):
            stage.move_by({"x": 1.0})
        with pytest.raises(PositionError):
            stage.jog("x", 1)
        stage.home()
        assert stage.is_homed
        stage.move_to({"x": 1.0})


def test_homing_is_forgotten_on_close():
    stage = StubStage(requires_homing=True)
    with stage:
        stage.home()
    with stage:
        assert not stage.is_homed


def test_stop_does_not_wait_for_the_device_lock(stage):
    holding = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with stage._lock:
            holding.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    holding.wait(timeout=5)
    stopper = threading.Thread(target=stage.stop)
    stopper.start()
    stopper.join(timeout=1)
    stopped = not stopper.is_alive()
    release.set()
    holder.join()
    assert stopped
    assert ("stop",) in stage.calls


def test_position_returns_a_copy(stage):
    stage.position()["x"] = 99.0
    assert stage.pos["x"] == 0.0


def test_stage_methods_need_open_device():
    stage = StubStage()
    for call in (stage.stop, stage.position):
        with pytest.raises(NotConnected):
            call()
    with pytest.raises(NotConnected):
        stage.move_to({"x": 1.0})
