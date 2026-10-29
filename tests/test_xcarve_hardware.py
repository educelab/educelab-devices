"""X-Carve tests against a real gantry: ``pytest -m hardware tests/test_xcarve_hardware.py -s``.

Uses the one X-Controller attached, or ``XCARVE_SERIAL_NUMBER`` to pick one.
Opening the port resets the controller, so don't run these while anything else
is using it. Tests that move the gantry also need ``XCARVE_ALLOW_MOTION=1``;
they home it, then move at 10 mm/s, never more than 40 mm from home. Clear the
bed and keep a hand near the power switch. Run with ``-s`` to see the
measurements these tests print for the hardware doc.
"""

import os
import threading
import time

import pytest

from educelab.devices.errors import (
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    PositionError,
)
from educelab.devices.stages.xcarve import ARRIVAL_TOLERANCE_MM, XCarve

pytestmark = pytest.mark.hardware

# pgs-scan's machine bounds, as el-devices xcarve uses by default.
LIMITS = {"x": (-699.0, 0.0), "y": (-699.0, 0.0), "z": (-45.0, -1.0)}
SPEED = 10.0
STEP = 10.0

needs_motion = pytest.mark.skipif(
    os.environ.get("XCARVE_ALLOW_MOTION") != "1",
    reason="moves the gantry; set XCARVE_ALLOW_MOTION=1 to run",
)


@pytest.fixture(scope="module")
def gantry():
    stage = XCarve(
        serial_number=os.environ.get("XCARVE_SERIAL_NUMBER"),
        limits=LIMITS,
        speed=SPEED,
    )
    try:
        stage.open()
    except DeviceNotFound as exc:
        pytest.skip(f"no X-Controller: {exc}")
    print(f"\n{stage.info}")
    yield stage
    stage.stop()
    stage.close()


@pytest.fixture(scope="module")
def homing_failure():
    """Why homing failed, once it has; later tests skip rather than retry."""
    return []


@pytest.fixture
def homed(gantry, homing_failure):
    """The gantry, homed, at the slow test speed; stopped afterwards.

    Homing is tried once per run. Retrying after a homing alarm drives the
    axes at their switches again, which on the rig hit a broken Z switch.
    """
    if homing_failure:
        pytest.skip(f"homing failed earlier: {homing_failure[0]}")
    if not gantry.is_homed:
        started = time.monotonic()
        try:
            gantry.home()
        except DeviceError as exc:
            homing_failure.append(exc)
            raise
        print(f"\nhomed in {time.monotonic() - started:.1f} s")
    gantry.set_speed(SPEED)
    yield gantry
    gantry.stop()


def toward_middle(stage, axis_name, distance):
    """A target ``distance`` from the current position, toward the axis middle."""
    axis = next(a for a in stage.axes if a.name == axis_name)
    here = stage.position()[axis_name]
    middle = (axis.minimum + axis.maximum) / 2
    return here + (distance if middle > here else -distance)


# Without motion


def test_open_reports_grbl_and_its_rates(gantry):
    assert gantry.info.firmware_version.startswith("1.1")
    print(f"\nmaximum rates, mm/s: {gantry.max_rates()}")
    print(f"settings: {sorted(gantry._settings.items())}")
    assert set(gantry.max_rates()) == {"x", "y", "z"}


def test_position_is_reported_without_homing(gantry):
    position = gantry.position()
    print(f"\nposition after open: {position}")
    assert set(position) == {"x", "y", "z"}


def test_moves_before_homing_are_refused(gantry):
    if gantry.is_homed:
        pytest.skip("already homed by an earlier test")
    with pytest.raises(PositionError, match="homed"):
        gantry.move_to({"x": -5.0})


# With motion


@needs_motion
def test_homing_finds_home(homed):
    print(f"\nposition after homing: {homed.position()}")
    assert homed.is_homed


@needs_motion
@pytest.mark.parametrize("axis", ["x", "y", "z"])
def test_each_axis_moves_off_home_and_back(homed, axis):
    start = homed.position()[axis]
    target = toward_middle(homed, axis, STEP)
    started = time.monotonic()
    homed.move_to({axis: target})
    print(f"\n{axis} {STEP:g} mm took {time.monotonic() - started:.2f} s")
    assert homed.position()[axis] == pytest.approx(target, abs=ARRIVAL_TOLERANCE_MM)
    homed.move_to({axis: start})
    assert homed.position()[axis] == pytest.approx(start, abs=ARRIVAL_TOLERANCE_MM)


@needs_motion
def test_stop_interrupts_a_move(homed):
    start = homed.position()["x"]
    target = toward_middle(homed, "x", 4 * STEP)
    raised = []

    def move():
        try:
            homed.move_to({"x": target})
        except PositionError as exc:
            raised.append(exc)

    thread = threading.Thread(target=move)
    thread.start()
    time.sleep(1.0)
    homed.stop()
    thread.join(timeout=10)
    stopped_at = homed.position()["x"]
    print(f"\nstopped at x={stopped_at} on the way from {start} to {target}")
    assert raised
    assert min(start, target) < stopped_at < max(start, target)
    homed.move_to({"x": start})


@needs_motion
def test_jog_then_stop(homed):
    start = homed.position()["y"]
    direction = 1 if toward_middle(homed, "y", 1.0) > start else -1
    homed.jog("y", direction)
    time.sleep(0.5)
    homed.stop()
    time.sleep(0.5)
    moved = abs(homed.position()["y"] - start)
    print(f"\njog for 0.5 s at {SPEED:g} mm/s moved {moved:.2f} mm")
    assert 0 < moved < 2 * STEP
    homed.move_to({"y": start})


@needs_motion
def test_a_move_past_its_timeout_is_cancelled(homed):
    start = homed.position()["x"]
    target = toward_middle(homed, "x", 4 * STEP)
    with pytest.raises(DeviceTimeout):
        homed.move_to({"x": target}, timeout=1.0)
    time.sleep(0.5)
    after = homed.position()["x"]
    print(f"\ntimed out at x={after} on the way to {target}")
    assert min(start, target) < after < max(start, target)
    homed.move_to({"x": start})


@needs_motion
def test_targets_outside_the_limits_never_move(homed):
    before = homed.position()
    with pytest.raises(PositionError, match="outside"):
        homed.move_to({"x": 1.0})
    assert homed.position() == pytest.approx(before, abs=ARRIVAL_TOLERANCE_MM)
