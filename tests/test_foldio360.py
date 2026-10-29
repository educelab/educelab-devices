"""Offline tests for the Foldio360 driver, against a fake transport.

They cover the command/response pairing hazards and the client-side validation
documented in docs/hardware/foldio360.md.
"""

import ast
import asyncio
import math
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from contracts import LightContract, StageContract
from educelab.devices.errors import (
    CommandRejected,
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    NotConnected,
    PositionError,
)
from educelab.devices.lights import Channel, ChannelKind, Light
from educelab.devices.stages import Stage
from educelab.devices.stages import foldio360 as f
from educelab.devices.stages.foldio360 import Foldio360
from fakes.foldio360 import FIRMWARE, TIMEOUT, FakeNusTransport, fake_bleak


@pytest.fixture(autouse=True)
def fast_settle(monkeypatch):
    """Shrink the driver's waits for stray replies and between cancels."""
    monkeypatch.setattr(f, "_SETTLE_QUIET_S", 0.02)
    monkeypatch.setattr(f, "_CANCEL_SPACING_S", 0.0)
    monkeypatch.setattr(f, "ACK_TIMEOUT_S", 0.2)


def table(replies=None, **fake) -> tuple[Foldio360, FakeNusTransport]:
    """An open turntable on a fake transport, with the open traffic cleared."""
    transport = FakeNusTransport(replies, **fake)
    dev = Foldio360(transport=transport)
    dev.open()
    transport.sent.clear()
    return dev, transport


def sequence(*replies: str):
    """A scripted reply that steps through ``replies``, then repeats the last."""
    remaining = list(replies)
    return lambda: remaining.pop(0) if len(remaining) > 1 else remaining[0]


def connected_transport() -> f.NusTransport:
    """A real NusTransport that believes it is connected, for framing tests."""
    transport = f.NusTransport()
    transport._client = type("Link", (), {"is_connected": True})()
    return transport


# -- the stage ---------------------------------------------------------------


FOLDIO_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
ADVERTISERS = [
    ("AA:01", "Foldio360", (FOLDIO_UUID,), -60),
    ("AA:02", None, (FOLDIO_UUID.upper(),), -80),  # found by service UUID alone
    ("AA:03", "Headphones", (), -40),
    ("AA:04", None, (), None),  # no name and no RSSI
]


class TestFoldio360Contract(StageContract):
    @pytest.fixture(autouse=True)
    def no_radio(self, monkeypatch):
        monkeypatch.setattr(f, "_bleak", fake_bleak(ADVERTISERS))

    @pytest.fixture
    def make_device(self):
        return lambda: Foldio360(transport=FakeNusTransport(angle=17))

    @pytest.fixture
    def position_tolerance(self) -> float:
        return float(f._ARRIVAL_TOLERANCE_DEG)


def test_is_a_stage_with_one_wrapping_rotary_axis():
    dev = Foldio360(transport=FakeNusTransport())
    assert isinstance(dev, Stage)
    (axis,) = dev.axes
    assert axis.name == "theta"
    assert axis.unit == "deg"
    assert axis.wraps
    assert not dev.can_home
    assert not dev.requires_homing


def test_constructor_touches_nothing():
    transport = FakeNusTransport()
    Foldio360(transport=transport)
    assert not transport.connected
    assert transport.sent == []


def test_open_reads_firmware_and_position():
    transport = FakeNusTransport(angle=123)
    with Foldio360(transport=transport) as dev:
        assert transport.sent == ["get_status", "get_position"]
        assert dev.info.firmware_version == FIRMWARE
        assert dev.info.backend == "foldio360"
        assert dev.info.address == "FA:KE"
        assert dev.position() == {"theta": 123.0}
    assert not transport.connected


def test_home_is_not_supported():
    dev, t = table()
    with pytest.raises(NotImplementedError):
        dev.home()
    assert t.sent == []


def test_no_origin_api():
    dev = Foldio360(transport=FakeNusTransport())
    for name in ("origin", "set_origin", "clear_origin", "get_position"):
        assert not hasattr(dev, name), name


@pytest.mark.parametrize(
    ("current", "target", "command"),
    [
        (10, 350, "rotate(CCW,20,1,1)"),  # not +340
        (350, 10, "rotate(CW,20,1,1)"),  # not -340
        (0, 180, "rotate(CCW,180,1,1)"),  # a tie, broken counter-clockwise
        (0, 450, "rotate(CW,90,1,1)"),  # absolute: 450 is 90
    ],
)
def test_move_to_takes_the_short_way(current, target, command):
    dev, t = table(angle=current)
    dev.move_to({"theta": target})
    assert t.sent == ["get_position", command, "get_position"]
    assert dev.position()["theta"] == target % 360


def test_move_to_current_position_sends_no_rotation():
    dev, t = table(angle=90)
    dev.move_to({"theta": 90.0})
    assert t.sent == ["get_position"]


def test_move_to_rounds_to_whole_degrees():
    dev, t = table()
    dev.move_to({"theta": 44.6})
    assert "rotate(CW,45,1,1)" in t.sent
    assert dev.position() == {"theta": 45.0}
    dev.move_to({"theta": 45.4})
    assert t.sent.count("rotate(CW,45,1,1)") == 1, "45.4 rounds to where it is"


def test_move_by_is_literal():
    dev, t = table()
    dev.move_by({"theta": 720.0})
    rotations = [c for c in t.sent if c.startswith("rotate")]
    assert rotations == ["rotate(CW,360,1,1)", "rotate(CW,360,1,1)"], "+720 turns twice"
    assert dev.position() == {"theta": 0.0}


def test_move_by_negative_and_rounded():
    dev, t = table(angle=100)
    dev.move_by({"theta": -25.4})
    assert "rotate(CCW,25,1,1)" in t.sent
    assert dev.position() == {"theta": 75.0}


def test_move_by_less_than_half_a_degree_sends_nothing():
    dev, t = table()
    dev.move_by({"theta": 0.4})
    assert not [c for c in t.sent if c.startswith("rotate")]


def test_large_turns_split_into_legs_of_at_most_one_revolution():
    dev, t = table()
    dev.move_by({"theta": -800.0})
    rotations = [c for c in t.sent if c.startswith("rotate")]
    assert rotations == [
        "rotate(CCW,360,1,1)",
        "rotate(CCW,360,1,1)",
        "rotate(CCW,80,1,1)",
    ]
    assert dev.position() == {"theta": 280.0}


def test_arrival_is_verified():
    dev, t = table()
    replies = sequence("0", "50")  # before the move, then 40 degrees short
    t._replies["get_position"] = replies
    with pytest.raises(PositionError, match="50"):
        dev.move_to({"theta": 90.0})
    assert dev.position() == {"theta": 50.0}


def test_arrival_within_one_degree_is_accepted():
    dev, t = table()
    replies = sequence("0", "91")
    t._replies["get_position"] = replies
    dev.move_to({"theta": 90.0})
    assert dev.position() == {"theta": 91.0}


def test_arrival_tolerance_wraps_through_zero():
    dev, t = table(angle=10)
    replies = sequence("10", "359")
    t._replies["get_position"] = replies
    dev.move_to({"theta": 0.0})
    assert dev.position() == {"theta": 359.0}


def test_default_timeout_comes_from_the_timing_model(monkeypatch):
    limits = []
    dev, t = table()
    real_read = t.read

    def read(timeout):
        limits.append(timeout)
        return real_read(timeout)

    monkeypatch.setattr(t, "read", read)
    monkeypatch.setattr(f, "_POLL_S", 1e9)  # one read per wait, so we see its limit
    dev.move_to({"theta": 90.0})
    assert max(limits) == pytest.approx(f.watchdog_seconds(90, 1), abs=0.05)


def test_move_timeout_replaces_the_timing_model():
    dev, _ = table(hold_moves=True)
    dev.move_timeout = 0.1
    started = time.monotonic()
    with pytest.raises(DeviceTimeout):
        dev.move_to({"theta": 90.0})
    # The timing model would allow several seconds.
    assert time.monotonic() - started < 1.0


def test_explicit_timeout_bounds_the_move():
    dev, t = table(hold_moves=True)
    with pytest.raises(DeviceTimeout):
        dev.move_to({"theta": 90.0}, timeout=0.1)
    assert "cancel(1)" in t.sent, "a move that timed out is cancelled"
    assert dev.position() == {"theta": 45.0}, "and its position re-read"


def test_timeout_drops_late_replies():
    dev, t = table(hold_moves=True, cancel_acks_move=True)
    with pytest.raises(DeviceTimeout):
        dev.move_to({"theta": 90.0}, timeout=0.1)
    t.hold_moves = False
    dev.move_to({"theta": 100.0})
    assert dev.position() == {"theta": 100.0}


def test_speed_lists_the_three_firmware_speeds_in_degrees_per_second():
    dev, _ = table()
    speed = dev.speed()
    assert speed.name == "speed"
    assert speed.unit == "deg/s"
    assert speed.choices == tuple(1 / f.SLOPE_S_PER_DEG[s] for s in (1, 2, 3))
    assert speed.value == speed.choices[0]


def test_set_speed_applies_to_later_moves():
    dev, t = table()
    fastest = max(dev.speed().choices)
    dev.set_speed(fastest)
    assert dev.speed().value == fastest
    dev.move_by({"theta": 25.0})
    assert "rotate(CW,25,3,1)" in t.sent


def test_set_speed_rejects_other_speeds():
    dev, _ = table()
    with pytest.raises(ValueError):
        dev.set_speed(10.0)


def test_fail_raises_command_rejected():
    dev, _ = table({"rotate(CW,90,1,1)": "FAIL"})
    with pytest.raises(CommandRejected):
        dev.move_to({"theta": 90.0})


def test_unexpected_reply_to_a_move():
    dev, _ = table({"rotate(CW,90,1,1)": "7"})
    with pytest.raises(f.UnexpectedResponse):
        dev.move_to({"theta": 90.0})


def test_position_out_of_range_is_unexpected():
    with pytest.raises(f.UnexpectedResponse):
        Foldio360(transport=FakeNusTransport({"get_position": "400"})).open()


def test_open_failure_closes_the_transport():
    transport = FakeNusTransport({"get_status": "FAIL"})
    dev = Foldio360(transport=transport)
    with pytest.raises(CommandRejected):
        dev.open()
    assert not dev.is_open
    assert not transport.connected


def test_rotate_sends_no_trailing_cancel():
    """The app's post-move cancel is bookkeeping, not protocol."""
    dev, t = table()
    dev.move_to({"theta": 90.0})
    assert not [c for c in t.sent if c.startswith("cancel")]


# -- the halo ----------------------------------------------------------------------


class TestFoldio360HaloContract(LightContract):
    """The halo on a closed turntable, which opening the halo opens."""

    @pytest.fixture
    def make_device(self):
        return lambda: Foldio360(transport=FakeNusTransport()).halo


class TestFoldio360HaloOnAnOpenTurntableContract(LightContract):
    """The halo on a turntable that something else opened and keeps open."""

    @pytest.fixture
    def make_device(self, request):
        def make():
            turntable = Foldio360(transport=FakeNusTransport())
            turntable.open()
            request.addfinalizer(turntable.close)
            return turntable.halo

        return make


def test_halo_is_a_light_with_one_mono_channel():
    dev = Foldio360(transport=FakeNusTransport())
    assert isinstance(dev.halo, Light)
    assert dev.halo is dev.halo
    assert dev.halo.channels == (Channel("halo", ChannelKind.MONO),)


@pytest.mark.parametrize(
    ("level", "command"),
    [
        (0.0, "set_bright(0)"),
        (0.5, "set_bright(50)"),
        (0.333, "set_bright(33)"),
        (1.0, "set_bright(100)"),
    ],
)
def test_halo_level_maps_to_percent(level, command):
    dev, t = table()
    dev.halo.open()
    dev.halo.set_level("halo", level)
    assert t.sent == [command]


def test_halo_level_is_read_from_the_device():
    dev, t = table()
    dev.halo.open()
    t.brightness = 37
    assert dev.halo.level("halo") == pytest.approx(0.37)
    assert t.sent == ["get_bright"]


def test_halo_open_opens_a_closed_turntable_and_close_closes_it():
    dev = Foldio360(transport=FakeNusTransport())
    assert not dev.halo.is_open
    with dev.halo:
        assert dev.is_open
        dev.halo.set_level("halo", 0.2)
    assert not dev.is_open


def test_halo_close_leaves_a_turntable_it_did_not_open():
    dev, t = table()
    dev.halo.open()
    dev.halo.set_level("halo", 0.2)
    dev.halo.close()
    assert dev.is_open
    assert not dev.halo.is_open
    with pytest.raises(NotConnected):
        dev.halo.set_level("halo", 0.2)
    dev.halo.open()
    dev.halo.set_level("halo", 0.3)
    assert t.brightness == 30


def test_halo_is_closed_until_opened():
    dev, t = table()
    assert not dev.halo.is_open
    with pytest.raises(NotConnected):
        dev.halo.set_level("halo", 0.2)
    assert t.sent == []


def test_closing_the_turntable_closes_the_halo():
    dev, _ = table()
    dev.halo.open()
    dev.close()
    assert not dev.halo.is_open
    with pytest.raises(NotConnected):
        dev.halo.set_level("halo", 0.2)
    dev.halo.open()  # reopens the turntable, and now owns it
    assert dev.is_open
    dev.halo.close()
    assert not dev.is_open


def test_turntable_opened_by_the_halo_stays_owned_after_a_reopen():
    dev = Foldio360(transport=FakeNusTransport())
    dev.halo.open()
    dev.halo.close()
    dev.open()
    dev.halo.open()
    dev.halo.close()
    assert dev.is_open, "the second open found the turntable already open"


def test_halo_info_is_the_turntables():
    with Foldio360(transport=FakeNusTransport()) as dev:
        assert dev.halo.info == dev.info


def test_halo_writes_are_paced():
    dev, t = table()
    dev.halo.open()
    start = time.monotonic()
    for level in (0.1, 0.2, 0.3):
        dev.halo.set_level("halo", level)
    assert time.monotonic() - start >= 2 * f._BRIGHT_PACING_S * 0.9
    assert t.brightness == 30


def test_halo_tolerates_a_lost_ack_when_the_level_landed(monkeypatch):
    monkeypatch.setattr(f, "_BRIGHT_ACK_S", 0.05)
    dev, t = table(lost_acks={"set_bright(50)"})
    dev.halo.open()
    dev.halo.set_level("halo", 0.5)
    assert t.sent == ["set_bright(50)", "get_bright"]
    assert dev.halo.level("halo") == 0.5


def test_halo_raises_when_the_ack_is_lost_and_the_level_did_not_land(monkeypatch):
    monkeypatch.setattr(f, "_BRIGHT_ACK_S", 0.05)
    dev, _ = table({"set_bright(50)": TIMEOUT})
    dev.halo.open()
    with pytest.raises(DeviceTimeout):
        dev.halo.set_level("halo", 0.5)


def test_a_late_halo_ack_is_drained(monkeypatch):
    monkeypatch.setattr(f, "_BRIGHT_ACK_S", 0.05)
    monkeypatch.setattr(f, "_SETTLE_QUIET_S", 0.2)
    dev, t = table(ack_delays={"set_bright(50)": 0.1})
    dev.halo.open()
    dev.halo.set_level("halo", 0.5)
    t.angle = 42
    assert dev.position() == {"theta": 42.0}, "the late OK isn't read as a position"


def test_halo_works_inside_a_run():
    dev, t = table()
    dev.halo.open()
    dev.run(24, lambda index: dev.halo.set_level("halo", index / 100) or False)
    assert "set_bright(1)" in t.sent


# -- protocol helpers and the transport ---------------------------------------


def test_driver_touches_no_disk():
    """The driver holds state in memory only (ADR 0003)."""
    tree = ast.parse(Path(f.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = imported & {
        "pathlib",
        "os",
        "shutil",
        "json",
        "pickle",
        "shelve",
        "sqlite3",
        "tempfile",
        "configparser",
    }
    assert not forbidden, f"driver must not reach the filesystem, imports {forbidden}"
    calls = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "open" not in calls


def test_timing_model_reproduces_the_published_fit():
    assert f.predicted_move_seconds(380, 1) == pytest.approx(52.18, abs=0.02)
    assert f.predicted_move_seconds(90, 1) == pytest.approx(12.47, abs=0.05)
    assert f.watchdog_seconds(90, 3) > f.predicted_move_seconds(90, 3)


def test_a_fragmented_integer_is_not_truncated():
    """`3` is both a whole reply and the head of `302`."""
    t = connected_transport()
    t._chunks.put(b"3")
    t._chunks.put(b"02")
    assert t.read(1.0) == "302"
    t._chunks.put(b"301")
    assert t.read(1.0) == "301"
    t._chunks.put(b"OK")
    assert t.read(1.0) == "OK"


def test_transport_read_times_out_with_device_timeout():
    t = connected_transport()
    with pytest.raises(DeviceTimeout):
        t.read(0.01)


def test_transport_close_never_raises():
    t = f.NusTransport()
    t.close()  # never connected: a no-op
    t.close()


def test_missing_bleak_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "bleak", None)
    with pytest.raises(ImportError, match=r"educelab-devices\[foldio360\]"):
        f.scan_lines(0.1)


def test_importing_the_driver_does_not_import_bleak():
    code = (
        "import sys, educelab.devices.stages.foldio360; "
        "sys.exit('bleak' in sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


# -- jog and stop ---------------------------------------------------------------


def in_thread(func, *args, **kwargs) -> tuple[threading.Thread, list]:
    outcome: list = []

    def target() -> None:
        try:
            outcome.append(func(*args, **kwargs))
        except DeviceError as exc:
            outcome.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


@pytest.mark.parametrize(("direction", "wire"), [(1, "CW"), (-1, "CCW")])
def test_jog_acks_on_spin_up(direction, wire):
    dev, t = table()
    dev.set_speed(dev.speed().choices[1])
    dev.jog("theta", direction)
    assert t.sent == [f"rotate({wire},0,2,0)"]
    assert t.jogging


def test_stop_cancels_a_jog_by_its_mode():
    dev, t = table()
    dev.jog("theta", 1)
    dev.stop()
    assert t.sent[-1] == "cancel(0)"
    assert "cancel(1)" not in t.sent
    assert not t.jogging


def test_stop_when_idle_sends_both_cancels():
    """Motion started elsewhere has an unknown mode, so stop() covers both."""
    dev, t = table()
    dev.stop()
    assert t.sent == ["cancel(0)", "cancel(1)"]


def test_stop_survives_a_lost_cancel_ack():
    dev, t = table({"cancel(0)": TIMEOUT})
    dev.stop()
    assert t.sent == ["cancel(0)", "cancel(1)"]
    dev.move_to({"theta": 10.0})
    assert dev.position() == {"theta": 10.0}


def test_jog_while_jogging_raises_busy():
    dev, _ = table()
    dev.jog("theta", 1)
    with pytest.raises(DeviceBusy):
        dev.jog("theta", -1)


def test_move_while_jogging_raises_busy():
    dev, t = table()
    dev.jog("theta", 1)
    with pytest.raises(DeviceBusy):
        dev.move_to({"theta": 90.0})
    with pytest.raises(DeviceBusy):
        dev.move_by({"theta": 90.0})
    assert not [c for c in t.sent if c.startswith("rotate(CW,90")]


def test_jog_without_ack_is_cancelled():
    dev, t = table({"rotate(CW,0,1,0)": TIMEOUT})
    with pytest.raises(DeviceTimeout):
        dev.jog("theta", 1)
    assert t.sent[-1] == "cancel(0)"


def test_close_cancels_a_jog():
    dev, t = table()
    dev.jog("theta", 1)
    dev.close()
    assert t.sent[-1] == "cancel(0)"
    assert not t.connected


@pytest.mark.parametrize("cancel_acks_move", [True, False])
def test_stop_from_another_thread_interrupts_a_move(cancel_acks_move):
    dev, t = table(hold_moves=True, cancel_acks_move=cancel_acks_move)
    thread, outcome = in_thread(dev.move_to, {"theta": 90.0})
    assert t.move_started.wait(timeout=2)
    dev.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert isinstance(outcome[0], PositionError)
    assert t.sent.count("cancel(1)") == 1
    assert dev.position() == {"theta": 45.0}, "position is re-read after the stop"
    # Whatever acks the cancel produced are drained, not read as replies.
    t.hold_moves = False
    dev.move_to({"theta": 100.0})
    assert dev.position() == {"theta": 100.0}


def test_stop_is_cleared_by_the_next_move():
    dev, t = table(hold_moves=True)
    thread, _ = in_thread(dev.move_to, {"theta": 90.0})
    assert t.move_started.wait(timeout=2)
    dev.stop()
    thread.join(timeout=2)
    t.hold_moves = False
    dev.move_by({"theta": 10.0})
    assert dev.position() == {"theta": 55.0}


def test_stop_while_another_command_holds_the_lock_cancels_a_jog():
    dev, t = table()
    dev.jog("theta", 1)
    holding, release = threading.Event(), threading.Event()

    def hold_lock() -> None:
        with dev._lock:
            holding.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert holding.wait(timeout=2)
    dev.stop()  # must not wait for the lock
    release.set()
    holder.join()
    assert t.sent[-1] == "cancel(0)"
    assert not t.jogging
    t.angle = 30
    assert dev.position() == {"theta": 30.0}, (
        "the cancel's ack isn't read as a position"
    )


# -- position ---------------------------------------------------------------------


def test_position_during_a_move_is_the_last_known_and_does_not_block():
    dev, t = table(angle=20, hold_moves=True)
    thread, _ = in_thread(dev.move_to, {"theta": 90.0})
    assert t.move_started.wait(timeout=2)
    sent = len(t.sent)
    start = time.monotonic()
    assert dev.position() == {"theta": 20.0}
    assert time.monotonic() - start < 0.1
    assert len(t.sent) == sent, "nothing is sent while the move waits"
    t.finish_move()
    thread.join(timeout=2)
    assert dev.position() == {"theta": 90.0}


def test_position_when_idle_reads_the_counter():
    dev, t = table()
    t.angle = 77  # e.g. moved by another client
    assert dev.position() == {"theta": 77.0}
    assert t.sent == ["get_position"]


def test_position_follows_a_jog():
    dev, t = table()
    dev.jog("theta", 1)
    t.angle = 15
    assert dev.position() == {"theta": 15.0}
    dev.stop()


# -- the device lock ----------------------------------------------------------------


def test_commands_wait_for_a_move_to_finish():
    """One command at a time: a second thread's command waits for the move."""
    dev, t = table(hold_moves=True)
    dev.halo.open()
    mover, _ = in_thread(dev.move_to, {"theta": 90.0})
    assert t.move_started.wait(timeout=2)
    lighter, outcome = in_thread(dev.halo.set_level, "halo", 0.4)
    time.sleep(0.1)
    assert lighter.is_alive(), "the halo must wait for the shared device lock"
    assert "set_bright(40)" not in t.sent
    t.finish_move()
    mover.join(timeout=2)
    lighter.join(timeout=2)
    assert outcome == [None]
    assert t.sent.index("set_bright(40)") > t.sent.index("rotate(CW,90,1,1)")


def test_commands_on_a_closed_device_raise_not_connected():
    dev = Foldio360(transport=FakeNusTransport())
    with pytest.raises(NotConnected):
        dev.halo.set_level("halo", 0.1)
    with pytest.raises(NotConnected):
        dev.move_to({"theta": 10.0})


# -- run, IR shutter, set_signal (ported from the original client's tests) --------


def run_frames(dev: Foldio360, frames: int, stop_at: int | None = None) -> list[int]:
    seen: list[int] = []

    def on_frame(index: int) -> bool:
        seen.append(index)
        return index != stop_at

    dev.run(frames, on_frame)
    return seen


@pytest.mark.parametrize("frames", [0, -3, 1.5, True])
def test_run_validates_frames(frames):
    dev, t = table()
    with pytest.raises(ValueError):
        dev.run(frames, lambda index: None)
    assert t.sent == []


def test_set_signal_takes_bytes():
    dev, t = table()
    with pytest.raises(TypeError):
        dev.set_signal("not bytes")
    assert t.sent == []


def test_run_sequence():
    frames = iter(["1", "2", "3", "0"])
    dev, t = table({"run(3,3)": lambda: next(frames), "continue": lambda: next(frames)})
    dev.set_speed(max(dev.speed().choices))
    assert run_frames(dev, 3) == [1, 2, 3]
    # continue is sent after the last frame too, to collect the 0 terminator
    assert t.sent[:4] == ["run(3,3)", "continue", "continue", "continue"]


def test_run_through_the_emulated_firmware():
    dev, t = table()
    assert run_frames(dev, 24) == list(range(1, 25))
    assert t.sent.count("continue") == 24
    # Frame 1 is the starting position, so 24 frames are 23 steps of 15 degrees
    # in the emulation; whether the real turntable steps on to 360 is unknown.
    assert dev.position() == {"theta": 345.0}


def test_run_abort_cancels():
    frames = iter(["1", "2", "3", "0"])
    dev, t = table({"run(3,1)": lambda: next(frames), "continue": lambda: next(frames)})
    assert run_frames(dev, 3, stop_at=2) == [1, 2]
    assert "cancel(1)" in t.sent
    assert t.sent.count("continue") == 1


def test_run_cancels_when_on_frame_raises():
    dev, t = table()

    def on_frame(index: int) -> None:
        raise RuntimeError("camera failed")

    with pytest.raises(RuntimeError, match="camera failed"):
        dev.run(24, on_frame)
    assert "cancel(1)" in t.sent
    assert "continue" not in t.sent


def test_ir_shutter_echo():
    frames = iter(["1", "2", "0"])
    dev, t = table(
        {
            "run(2,3)": lambda: next(frames),
            "continue": lambda: next(frames),
            "ir_shutter(0)": "1",
        }
    )
    dev.set_speed(max(dev.speed().choices))
    shots: list[int] = []
    dev.run(2, lambda index: shots.append(dev.ir_shutter()))
    assert shots[0] == 1
    assert "ir_shutter(0)" in t.sent


def test_ir_shutter_needs_a_signal():
    dev, _ = table()
    with pytest.raises(CommandRejected):
        dev.ir_shutter()


def test_ir_shutter_argument_two_is_rejected():
    dev, _ = table()
    dev.set_signal(b"\x01")
    with pytest.raises(CommandRejected):
        dev.ir_shutter(2)


def test_set_signal_is_bytes():
    dev, t = table()
    blob = bytes([0x31, 0x32, 0x21, 0x03, 0x10, 0x27])
    dev.set_signal(blob)
    assert t.sent[0] == b"set_signal(" + blob + b")"
    assert t.signal == blob
    assert dev.ir_shutter() == 1


def test_long_commands_are_written_in_twenty_byte_chunks():
    writes = []

    class Client:
        async def write_gatt_char(self, char, data, response):
            writes.append((char, bytes(data), response))

    t = f.NusTransport()
    t._client = Client()
    blob = b"set_signal(" + bytes(range(1, 16)) + b")"
    asyncio.run(t._write(blob))
    assert [len(data) for _, data, _ in writes] == [20, 7]
    assert b"".join(data for _, data, _ in writes) == blob
    assert all(char == f.NUS_RX_CHAR and not response for char, _, response in writes)


def test_run_timeout_cancels_once(monkeypatch):
    """A second cancel inside ~100 ms can cost an acknowledgement."""
    monkeypatch.setattr(f, "watchdog_seconds", lambda degrees, speed: 0.05)
    dev, t = table()
    with pytest.raises(DeviceTimeout, match="24, 36, 48"):
        run_frames(dev, 12)  # wedges after frame 1
    assert t.sent.count("cancel(1)") == 1


def test_stop_from_another_thread_interrupts_a_run():
    dev, t = table()
    started, release = threading.Event(), threading.Event()

    def on_frame(index: int) -> None:
        started.set()
        release.wait(timeout=2)

    thread, outcome = in_thread(dev.run, 24, on_frame)
    assert started.wait(timeout=2)
    dev.stop()
    release.set()
    thread.join(timeout=2)
    assert isinstance(outcome[0], PositionError)
    assert t.sent.count("cancel(1)") == 1
    assert t.sent.count("continue") == 0
    dev.move_to({"theta": 30.0})
    assert dev.position() == {"theta": 30.0}


def test_moves_during_a_run_are_refused():
    dev, _ = table()
    errors = []

    def on_frame(index: int) -> bool:
        try:
            dev.move_by({"theta": 10.0})
        except DeviceBusy as exc:
            errors.append(exc)
        return False

    dev.run(24, on_frame)
    assert len(errors) == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("Bluetooth adapter not found"), DeviceNotFound),
        (TimeoutError(), DeviceTimeout),
    ],
)
def test_connection_failures_are_device_errors(monkeypatch, error, expected):
    monkeypatch.setattr(f, "_bleak", fake_bleak(connect_error=error))
    dev = Foldio360("AA:BB")
    with pytest.raises(expected) as caught:
        dev.open()
    # Python 3.11's run_coroutine_threadsafe copies a TimeoutError on the way out.
    assert isinstance(caught.value.__cause__, type(error))
    assert "AA:BB" in str(caught.value)
    assert not dev.is_open


def test_a_failed_write_means_the_link_is_gone():
    t = connected_transport()

    class Loop:
        def call(self, coro, timeout=None):
            coro.close()
            raise OSError("link lost")

    t._loop = Loop()
    with pytest.raises(NotConnected):
        t.send("get_status")


def test_loop_thread_timeout_is_a_device_timeout():
    loop = f._LoopThread()
    try:
        with pytest.raises(DeviceTimeout, match="no reply"):
            loop.call(asyncio.sleep(1), timeout=0.05)
    finally:
        loop.stop()


def test_discover_finds_turntables_by_name_or_service(monkeypatch):
    monkeypatch.setattr(f, "_bleak", fake_bleak(ADVERTISERS))
    infos = Foldio360.discover()
    assert [i.address for i in infos] == ["AA:01", "AA:02"]
    assert all(i.backend == "foldio360" and i.model == "Foldio360" for i in infos)


def test_discover_wraps_scan_failures(monkeypatch):
    monkeypatch.setattr(f, "_bleak", fake_bleak(scan_error=OSError("no adapter")))
    with pytest.raises(DeviceError, match="no adapter"):
        Foldio360.discover()


def test_scan_lines_lists_every_advertiser_strongest_first(monkeypatch):
    monkeypatch.setattr(f, "_bleak", fake_bleak(ADVERTISERS))
    lines = f.scan_lines(0.1)
    assert [line.split()[0] for line in lines] == ["AA:03", "AA:01", "AA:02", "AA:04"]
    assert lines[1].endswith("<-- Foldio360")
    assert "   - dBm  -" in lines[3]


# -- stop leaves the halo alone ----------------------------------------------------


def test_stop_when_idle_leaves_the_halo_alone():
    dev, t = table()
    dev.halo.open()
    dev.halo.set_level("halo", 0.4)
    sent = len(t.sent)
    dev.stop()
    assert not [c for c in t.sent[sent:] if str(c).startswith("set_bright")]
    assert dev.halo.level("halo") == pytest.approx(0.4)


def test_stop_mid_move_leaves_the_halo_alone():
    dev, t = table(hold_moves=True)
    dev.halo.open()
    dev.halo.set_level("halo", 0.4)
    sent = len(t.sent)
    thread, outcome = in_thread(dev.move_to, {"theta": 90.0})
    assert t.move_started.wait(timeout=2)
    dev.stop()
    thread.join(timeout=2)
    assert isinstance(outcome[0], PositionError)
    assert not [c for c in t.sent[sent:] if str(c).startswith("set_bright")]
    assert dev.halo.level("halo") == pytest.approx(0.4)


def test_stopping_a_jog_leaves_the_halo_alone():
    dev, _ = table()
    dev.halo.open()
    dev.halo.set_level("halo", 0.4)
    dev.jog("theta", 1)
    dev.stop()
    assert dev.halo.level("halo") == pytest.approx(0.4)


def test_infinite_timeout_waits_past_the_watchdog(monkeypatch):
    """The guide's math.inf means "wait indefinitely", not "use the default"."""
    monkeypatch.setattr(f, "watchdog_seconds", lambda degrees, speed: 0.05)
    dev, t = table(hold_moves=True)
    with pytest.raises(DeviceTimeout):
        dev.move_to({"theta": 90.0})  # the default: the (shrunk) watchdog
    t.move_started.clear()
    thread, outcome = in_thread(dev.move_to, {"theta": 90.0}, timeout=math.inf)
    assert t.move_started.wait(timeout=2)
    time.sleep(0.2)
    assert thread.is_alive(), "an infinite timeout must not use the watchdog"
    t.finish_move()
    thread.join(timeout=2)
    assert outcome == [None]
