import threading

import pytest

from contracts.device import DeviceContract
from educelab.devices.errors import NotConnected, PositionError
from educelab.devices.stages import Axis, AxisKind, Stage


def ready(stage: Stage) -> Stage:
    if stage.requires_homing and not stage.is_homed:
        stage.home()
    return stage


def reachable_target(axis: Axis, current: float) -> float:
    """A position inside the axis's limits, away from ``current``."""
    if axis.wraps:
        return (current + 90.0) % 360.0
    if axis.minimum is not None and axis.maximum is not None:
        span = axis.maximum - axis.minimum
        for fraction in (0.5, 0.25):
            target = axis.minimum + span * fraction
            if target != current:
                return target
    if axis.maximum is not None:
        return axis.maximum - 1.0
    if axis.minimum is not None:
        return axis.minimum + 1.0
    return current + 1.0


class StageContract(DeviceContract):
    """Rules every `Stage` follows."""

    @pytest.fixture
    def position_tolerance(self) -> float:
        """How close a finished move must land to its target."""
        return 1e-3

    def test_axes_have_unique_names(self, device):
        names = [axis.name for axis in device.axes]
        assert names
        assert len(names) == len(set(names))

    def test_position_reports_every_axis(self, device):
        assert set(device.position()) == {axis.name for axis in device.axes}

    def test_move_to_reaches_target(self, device, position_tolerance):
        ready(device)
        for axis in device.axes:
            target = reachable_target(axis, device.position()[axis.name])
            device.move_to({axis.name: target})
            assert device.position()[axis.name] == pytest.approx(
                target, abs=position_tolerance
            )

    def test_move_by_reaches_target(self, device, position_tolerance):
        ready(device)
        for axis in device.axes:
            start = device.position()[axis.name]
            target = reachable_target(axis, start)
            device.move_by({axis.name: target - start})
            assert device.position()[axis.name] == pytest.approx(
                target, abs=position_tolerance
            )

    def test_move_outside_limits_raises(self, device):
        ready(device)
        bounded = [axis for axis in device.axes if axis.maximum is not None]
        if not bounded:
            pytest.skip("no axis has a maximum")
        for axis in bounded:
            before = device.position()[axis.name]
            with pytest.raises(PositionError):
                device.move_to({axis.name: axis.maximum + 1.0})
            assert device.position()[axis.name] == before

    def test_wrapping_axis_reports_positions_below_360(
        self, device, position_tolerance
    ):
        ready(device)
        wrapping = [axis for axis in device.axes if axis.wraps]
        if not wrapping:
            pytest.skip("no wrapping axis")
        for axis in wrapping:
            device.move_to({axis.name: 370.0})
            assert device.position()[axis.name] == pytest.approx(
                10.0, abs=position_tolerance
            )
            device.move_to({axis.name: 350.0})
            assert device.position()[axis.name] == pytest.approx(
                350.0, abs=position_tolerance
            )
            device.move_by({axis.name: 20.0})
            assert 0.0 <= device.position()[axis.name] < 360.0
            assert device.position()[axis.name] == pytest.approx(
                10.0, abs=position_tolerance
            )

    def test_unknown_axis_raises_key_error(self, device):
        ready(device)
        with pytest.raises(KeyError):
            device.move_to({"no-such-axis": 0.0})

    def test_homing_rules(self, device):
        if not device.can_home:
            assert not device.requires_homing
            with pytest.raises(NotImplementedError):
                device.home()
            return
        axis = device.axes[0]
        if device.requires_homing:
            with pytest.raises(PositionError):
                device.move_to({axis.name: reachable_target(axis, 0.0)})
        assert not device.is_homed
        device.home()
        assert device.is_homed

    def test_wrapping_move_to_is_absolute(self, device):
        ready(device)
        wrapping = [axis for axis in device.axes if axis.wraps]
        if not wrapping:
            pytest.skip("no wrapping axis")
        for axis in wrapping:
            device.move_to({axis.name: 90.0})
            device.move_to({axis.name: 450.0})
            assert device.position()[axis.name] == pytest.approx(90.0, abs=1e-3)

    def test_jog_then_stop(self, device):
        ready(device)
        device.jog(device.axes[0].name, 1)
        device.stop()
        assert set(device.position()) == {axis.name for axis in device.axes}

    def test_speed_is_a_setting_named_speed(self, device):
        speed = device.speed()
        expected = (
            "mm/s"
            if any(axis.kind is AxisKind.LINEAR for axis in device.axes)
            else "deg/s"
        )
        assert speed.name == "speed"
        assert speed.unit == expected

    def test_set_speed_accepts_allowed_speeds(self, device):
        speed = device.speed()
        for direction in ("nearest", "down", "up"):
            device.set_speed(speed.nearest(speed.value, direction))
        device.set_speed(speed.value)
        assert device.speed().value == pytest.approx(speed.value)

    def test_speed_above_maximum_raises(self, device):
        speed = device.speed()
        top = max(speed.choices) if speed.choices else speed.maximum
        if top is None:
            pytest.skip("speed has no maximum")
        with pytest.raises(ValueError):
            device.set_speed(float(top) * 2 + 1)

    def test_stop_when_idle_is_harmless(self, device):
        device.stop()
        device.stop()

    def test_stop_does_not_wait_for_a_running_move(self, device):
        holding = threading.Event()
        release = threading.Event()

        def hold_lock() -> None:
            with device._lock:
                holding.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        try:
            assert holding.wait(timeout=5)
            stopper = threading.Thread(target=device.stop)
            stopper.start()
            stopper.join(timeout=1)
            assert not stopper.is_alive()
        finally:
            release.set()
            holder.join()

    def test_closed_stage_raises_not_connected(self, make_device):
        stage = make_device()
        with pytest.raises(NotConnected):
            stage.position()
        with pytest.raises(NotConnected):
            stage.stop()
