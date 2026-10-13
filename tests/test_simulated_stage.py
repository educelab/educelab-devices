import functools
import threading
import time

import pytest

from contracts import StageContract
from educelab.devices.errors import (
    DeviceBusy,
    DeviceError,
    DeviceTimeout,
    PositionError,
)
from educelab.devices.stages import Axis, AxisKind, SimulatedStage

FAST = 1e5


class TestSimulatedStage(StageContract):
    @pytest.fixture
    def make_device(self):
        return functools.partial(SimulatedStage, speed=FAST)


class TestSimulatedStageRequiringHoming(StageContract):
    @pytest.fixture
    def make_device(self):
        return functools.partial(SimulatedStage, speed=FAST, requires_homing=True)


def turntable(speed: float) -> SimulatedStage:
    return SimulatedStage(
        axes=(Axis("theta", AxisKind.ROTARY, wraps=True),), speed=speed
    )


def run_in_thread(func, *args, **kwargs) -> tuple[threading.Thread, list]:
    outcome: list = []

    def target() -> None:
        try:
            outcome.append(func(*args, **kwargs))
        except DeviceError as exc:
            outcome.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


def test_move_takes_simulated_time():
    with SimulatedStage(speed=200.0) as stage:
        start = time.monotonic()
        stage.move_to({"x": 20.0})
        assert time.monotonic() - start >= 0.09


def test_position_changes_during_move():
    with SimulatedStage(speed=100.0) as stage:
        thread, _ = run_in_thread(stage.move_to, {"x": 50.0})
        time.sleep(0.15)
        midway = stage.position()["x"]
        stage.stop()
        thread.join()
        assert 0.0 < midway < 50.0


def test_stop_interrupts_move_from_another_thread():
    with SimulatedStage(speed=100.0) as stage:
        thread, outcome = run_in_thread(stage.move_to, {"x": 100.0})
        time.sleep(0.1)
        stage.stop()
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert isinstance(outcome[0], PositionError)
        assert 0.0 < stage.position()["x"] < 100.0


def test_slow_move_times_out():
    with SimulatedStage(speed=10.0) as stage, pytest.raises(DeviceTimeout):
        stage.move_to({"x": 100.0}, timeout=0.1)


def test_wrapping_axis_takes_the_shorter_way():
    with turntable(speed=360.0) as stage:
        stage.move_to({"theta": 350.0})
        start = time.monotonic()
        stage.move_to({"theta": 10.0})
        assert time.monotonic() - start < 0.5
        assert stage.position()["theta"] == pytest.approx(10.0)


def test_move_by_on_wrapping_axis_is_literal():
    with turntable(speed=3600.0) as stage:
        start = time.monotonic()
        stage.move_by({"theta": 720.0})
        assert time.monotonic() - start >= 0.19
        assert stage.position()["theta"] == pytest.approx(0.0)


def test_jog_moves_until_stopped():
    with SimulatedStage(speed=100.0) as stage:
        stage.jog("x", 1)
        time.sleep(0.1)
        stage.stop()
        stopped_at = stage.position()["x"]
        time.sleep(0.05)
        assert stopped_at > 0.0
        assert stage.position()["x"] == stopped_at


def test_jog_stops_at_limit():
    with SimulatedStage(speed=FAST) as stage:
        stage.jog("x", -1)
        time.sleep(0.05)
        assert stage.position()["x"] == 0.0
        stage.stop()


def test_move_while_jogging_raises_busy():
    with SimulatedStage(speed=1.0) as stage:
        stage.jog("y", 1)
        with pytest.raises(DeviceBusy):
            stage.move_to({"x": 1.0})
        stage.stop()


class TestSimulatedStageWithoutReference(StageContract):
    @pytest.fixture
    def make_device(self):
        return functools.partial(SimulatedStage, speed=FAST, can_home=False)


def test_requires_homing_needs_can_home():
    with pytest.raises(ValueError):
        SimulatedStage(can_home=False, requires_homing=True)


def test_home_moves_axes_to_reference():
    with SimulatedStage(speed=FAST, requires_homing=True) as stage:
        stage.home()
        stage.move_to({"x": 10.0, "theta": 90.0})
        stage.home()
        assert stage.position() == {"x": 0.0, "y": 0.0, "theta": 0.0}
