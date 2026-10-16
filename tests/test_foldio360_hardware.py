"""Foldio360 tests against a real turntable: ``pytest -m hardware tests/test_foldio360_hardware.py -s``.

Connects to ``FOLDIO360_ADDRESS`` if set, otherwise scans for one. Close the
official app first. The platter turns, so clear anything that could fall off.
Run with ``-s`` to see the measurements these tests print for the hardware doc.
"""

import itertools
import os
import threading
import time

import pytest

from educelab.devices.errors import (
    CommandRejected,
    DeviceNotFound,
    DeviceTimeout,
    PositionError,
)
from educelab.devices.stages.foldio360 import (
    Foldio360,
    predicted_move_seconds,
)

pytestmark = pytest.mark.hardware

THETA = "theta"
TOLERANCE = 1.0  # the counter's resolution; see docs/hardware/foldio360.md


@pytest.fixture(scope="module")
def table():
    pytest.importorskip("bleak", reason="needs educelab-devices[foldio360]")
    turntable = Foldio360(os.environ.get("FOLDIO360_ADDRESS"))
    try:
        turntable.open()
    except (DeviceNotFound, DeviceTimeout) as exc:
        pytest.skip(f"no Foldio360: {exc}")
    print(f"\n{turntable.info}")
    yield turntable
    turntable.stop()
    turntable.close()


@pytest.fixture
def stage(table):
    """The turntable at its slowest speed, stopped afterwards whatever happens."""
    table.set_speed(table.speed().choices[0])
    yield table
    table.stop()
    table.set_speed(table.speed().choices[0])


@pytest.fixture
def halo(table):
    table.halo.open()
    yield table.halo
    table.halo.set_level("halo", 0.0)
    table.halo.close()


def theta(stage: Foldio360) -> float:
    return stage.position()[THETA]


def distance(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def timed(func, *args) -> float:
    start = time.monotonic()
    func(*args)
    return time.monotonic() - start


def test_identifies_as_a_foldio360(table):
    info = table.info
    assert info.model == "Foldio360"
    assert info.address
    assert info.firmware_version.startswith("V")


def test_position_is_a_whole_degree(stage):
    position = theta(stage)
    assert 0.0 <= position < 360.0
    assert position == int(position)


def test_move_to_arrives(stage):
    target = (theta(stage) + 30.0) % 360.0
    stage.move_to({THETA: target})
    assert distance(theta(stage), target) <= TOLERANCE


def test_move_to_takes_the_short_way(stage):
    start = theta(stage)
    seconds = timed(stage.move_to, {THETA: (start - 20.0) % 360.0})
    print(f"\nmove_to 20 degrees back: {seconds:.2f} s")
    assert seconds < predicted_move_seconds(90, 1), "it went the long way round"


def test_move_by_returns_to_the_start(stage):
    start = theta(stage)
    stage.move_by({THETA: 45.0})
    assert distance(theta(stage), start + 45.0) <= TOLERANCE
    stage.move_by({THETA: -45.0})
    assert distance(theta(stage), start) <= TOLERANCE


def test_move_by_is_literal_past_a_full_turn(stage):
    stage.set_speed(max(stage.speed().choices))
    start = theta(stage)
    seconds = timed(stage.move_by, {THETA: 380.0})
    print(f"\nmove_by 380 at speed 3: {seconds:.2f} s")
    assert distance(theta(stage), start + 20.0) <= TOLERANCE
    assert seconds > predicted_move_seconds(360, 3) * 0.8, "it took the short way"


@pytest.mark.parametrize("level", [1, 2, 3])
def test_each_speed_matches_the_timing_model(stage, level):
    """Speeds 2 and 3 are assumed in the hardware doc; this measures them."""
    stage.set_speed(stage.speed().choices[level - 1])
    seconds = timed(stage.move_by, {THETA: 90.0})
    predicted = predicted_move_seconds(90, level)
    print(
        f"\n90 degrees at speed {level}: {seconds:.3f} s, predicted {predicted:.3f} s"
    )
    assert seconds == pytest.approx(predicted, rel=0.25)


def test_stop_from_another_thread_halts_a_move(stage):
    """Settles whether cancel(1) stops a rotate in progress (an open question)."""
    start = theta(stage)
    outcome: list = []

    def move() -> None:
        try:
            stage.move_by({THETA: 180.0})
        except PositionError as exc:
            outcome.append(exc)

    mover = threading.Thread(target=move)
    began = time.monotonic()
    mover.start()
    time.sleep(3.0)
    stopped_after = time.monotonic() - began
    stage.stop()
    mover.join(timeout=10)
    assert not mover.is_alive()
    assert outcome and isinstance(outcome[0], PositionError)
    first = theta(stage)
    time.sleep(2.0)
    second = theta(stage)
    moved = distance(first, start)
    print(
        f"\nstopped after {stopped_after:.1f} s at {moved:.0f} "
        f"degrees of 180; still {second - first:+.0f} degrees two seconds later"
    )
    assert first == second, "the platter kept turning after stop()"
    assert 5.0 <= moved < 90.0


def test_jog_then_stop(stage):
    start = theta(stage)
    stage.jog(THETA, 1)
    time.sleep(2.0)
    stage.stop()
    first = theta(stage)
    time.sleep(1.0)
    assert theta(stage) == first, "the platter kept turning after stop()"
    print(f"\njog for 2 s at speed 1: {distance(first, start):.0f} degrees")
    assert distance(first, start) >= 5.0


def test_stop_leaves_the_halo_alone(stage, halo):
    """The driver never sends set_bright on stop; this checks the firmware's cancels."""
    halo.set_level("halo", 0.25)
    stage.stop()
    assert halo.level("halo") == pytest.approx(0.25)
    stage.jog(THETA, 1)
    time.sleep(1.0)
    stage.stop()
    assert halo.level("halo") == pytest.approx(0.25)


def test_run_steps_and_stops_early(stage):
    """Records which way run() turns (an open question)."""
    positions: list[float] = []

    def on_frame(index: int) -> bool:
        positions.append(theta(stage))
        return index < 3

    stage.set_speed(max(stage.speed().choices))
    stage.run(24, on_frame)
    steps = [(b - a + 180.0) % 360.0 - 180.0 for a, b in itertools.pairwise(positions)]
    print(f"\nrun(24) steps: {steps} (positive is clockwise)")
    assert len(positions) == 3
    assert all(abs(abs(step) - 15.0) <= TOLERANCE for step in steps)


def test_ir_shutter_without_a_signal_is_rejected(table):
    with pytest.raises(CommandRejected):
        table.ir_shutter()


@pytest.mark.parametrize("level", [1.0, 0.5, 0.25, 0.0])
def test_halo_levels_read_back(halo, level):
    halo.set_level("halo", level)
    assert halo.level("halo") == pytest.approx(level)


def test_halo_ramp_lands_its_last_level(halo):
    for step in range(21):
        halo.set_level("halo", step / 20)
    assert halo.level("halo") == pytest.approx(1.0)
