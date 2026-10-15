"""Offline tests for the Foldio360 driver, against a scripted fake transport.

They cover the command/response pairing hazards and the client-side validation
documented in docs/hardware/foldio360.md.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from educelab.devices.errors import CommandRejected, DeviceTimeout
from educelab.devices.stages import foldio360 as f
from educelab.devices.stages.foldio360 import CCW, CW, Foldio360
from fakes.foldio360 import TIMEOUT, FakeNusTransport


def table(replies=None) -> tuple[Foldio360, FakeNusTransport]:
    transport = FakeNusTransport(replies)
    return Foldio360(transport=transport), transport


def connected_transport() -> f.NusTransport:
    """A real NusTransport that believes it is connected, for framing tests."""
    transport = f.NusTransport()
    transport._client = type("Link", (), {"is_connected": True})()
    return transport


# -- validation: FAIL doesn't say which argument was wrong, so the client checks --


@pytest.mark.parametrize(
    "call",
    [
        lambda d: d.rotate("Left", 90),  # the app's label
        lambda d: d.rotate(CW, 90, speed=4),
        lambda d: d.rotate(CW, 0),  # untested on the device
        lambda d: d.rotate(CW, -5),
        lambda d: d.rotate(CW, 1.5),
        lambda d: d.set_brightness(101),
        lambda d: d.set_brightness(-1),
    ],
)
def test_invalid_arguments_raise_before_sending(call):
    dev, t = table()
    with pytest.raises(ValueError):
        call(dev)
    assert t.sent == []


def test_rotate_wire_format_passes_speed_for_any_angle():
    dev, t = table()
    dev.rotate(CCW, 25, speed=3)
    assert t.sent == ["rotate(CCW,25,3,1)"]


def test_rotate_sends_no_trailing_cancel():
    """The app's post-move cancel is bookkeeping, not protocol."""
    dev, t = table()
    dev.rotate(CW, 90, speed=1)
    assert t.sent == ["rotate(CW,90,1,1)"]


def test_position_is_read_from_the_device():
    dev, t = table({"get_position": "301"})
    assert dev.get_position() == 301
    assert t.sent == ["get_position"]
    assert not hasattr(dev, "set_position"), "position is read-only on the device"


def test_driver_touches_no_disk():
    """The driver holds state in memory only; an origin cached to disk outlives
    the firmware counter it refers to."""
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


def test_origin_shifts_the_frame():
    dev, _ = table({"get_position": "100"})
    assert dev.origin is None
    assert dev.get_position() == 100
    assert dev.set_origin() == 100
    assert dev.get_position() == 0
    assert dev.get_device_position() == 100
    dev.set_origin(30)
    assert dev.get_position() == 70
    dev.set_origin(150)
    assert dev.get_position() == 310  # wraps, never negative
    with pytest.raises(TypeError):
        dev.set_origin(1.5)
    dev.clear_origin()
    assert dev.origin is None
    assert dev.get_position() == 100


def test_origin_validates_on_assignment():
    dev, t = table({"get_position": "10"})
    with pytest.raises(TypeError):
        dev.origin = 1.5
    with pytest.raises(TypeError):
        dev.origin = "90"
    assert dev.origin is None, "a rejected assignment must not land"
    dev.origin = 400
    assert dev.origin == 40
    dev.origin = -30
    assert dev.origin == 330
    assert dev.get_position() == 40
    dev.origin = None
    assert dev.origin is None
    assert t.sent == ["get_position"], "setting an origin is client-side only"


def test_origin_zero_is_pinned_not_absent():
    dev, _ = table({"get_position": "0"})
    assert dev.set_origin() == 0
    assert dev.origin is not None, "0 must not read as 'no origin pinned'"
    dev.clear_origin()
    assert dev.origin is None


def test_home_goes_to_the_origin():
    dev, t = table({"get_position": "100"})
    dev.set_origin(90)
    assert dev.get_position() == 10
    assert dev.home(speed=3) == -10
    assert t.sent[-1] == "rotate(CCW,10,3,1)"


def test_rotate_by_signs():
    """Positive is clockwise, the direction get_position counts up."""
    dev, t = table()
    dev.rotate_by(25, speed=3)
    dev.rotate_by(-25, speed=3)
    assert t.sent == ["rotate(CW,25,3,1)", "rotate(CCW,25,3,1)"]
    dev.rotate_by(0)
    assert len(t.sent) == 2, "a zero turn sends nothing"
    with pytest.raises(TypeError):
        dev.rotate_by(1.5)


@pytest.mark.parametrize(
    ("current", "target", "expected"),
    [
        ("10", 350, -20),
        ("350", 10, 20),
        ("0", 180, -180),  # exact half-turn: a tie, broken counter-clockwise
        ("90", 90, 0),
    ],
)
def test_move_to_takes_the_short_way(current, target, expected):
    dev, t = table({"get_position": current})
    assert dev.move_to(target) == expected
    if expected == 0:
        assert t.sent == ["get_position"], "a zero move must not be sent"


def test_home_is_move_to_zero():
    dev, t = table({"get_position": "300"})
    assert dev.home(speed=2) == 60
    assert t.sent == ["get_position", "rotate(CW,60,2,1)"]


def test_fail_raises_command_rejected():
    dev, _ = table({"rotate(CW,90,1,1)": "FAIL"})
    with pytest.raises(CommandRejected):
        dev.rotate(CW, 90)
    dev2, _ = table({"get_position": "FAIL"})
    with pytest.raises(CommandRejected):
        dev2.get_position()


def test_get_brightness():
    dev, t = table({"get_bright": "37"})
    assert dev.get_brightness() == 37
    assert t.sent == ["get_bright"]


def test_timeout_stops_the_motor_then_raises():
    dev, t = table({"rotate(CW,90,1,1)": TIMEOUT})
    with pytest.raises(DeviceTimeout):
        dev.rotate(CW, 90)
    assert t.sent == ["rotate(CW,90,1,1)", "cancel(1)"]
    assert t.drained >= 1, "a late reply must be discarded"


def test_jog_acks_on_spin_up_and_cancel_picks_its_mode():
    dev, t = table()
    dev.jog(CW, speed=2)
    assert t.sent == ["rotate(CW,0,2,0)"]
    assert dev.jogging
    dev.cancel()
    assert t.sent[-1] == "cancel(0)"
    assert not dev.jogging
    dev2, t2 = table()
    dev2.cancel()
    assert t2.sent == ["cancel(1)"]


def test_cancel_mode_is_selectable():
    dev, t = table()
    dev.cancel(f.MODE_JOG)
    dev.cancel(f.MODE_FIXED)
    assert t.sent == ["cancel(0)", "cancel(1)"]


def test_stop_sends_both_cancels(monkeypatch):
    monkeypatch.setattr(f.time, "sleep", lambda _s: None)
    dev, t = table()
    dev.stop()
    assert t.sent == ["cancel(0)", "cancel(1)"]


def test_brightness_fire_and_forget_does_not_wait():
    dev, t = table({"set_bright(50)": TIMEOUT})
    dev.set_brightness(50, wait=False)  # would raise if it waited
    assert t.sent == ["set_bright(50)"]


def test_fire_and_forget_ack_is_not_read_as_the_next_reply():
    dev, t = table({"get_position": "42"})
    dev.set_brightness(50, wait=False)
    assert t.drained == 0, "the ack may still land; nothing to drain yet"
    assert dev.get_position() == 42
    assert t.drained == 1, "the stray ack must be cleared before the next command"
    dev.get_position()
    assert t.drained == 1, "and only once"


def test_unexpected_response():
    dev, _ = table({"get_status": "OK"})
    assert dev.get_status() == "OK"  # get_status is not OK-checked
    dev2, _ = table({"rotate(CW,90,1,1)": "7"})
    with pytest.raises(f.UnexpectedResponse):
        dev2.rotate(CW, 90)


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


def test_close_never_raises():
    class Exploding(FakeNusTransport):
        def close(self):
            raise RuntimeError("link already gone")

    dev = Foldio360(transport=Exploding())
    with pytest.raises(RuntimeError):
        dev.close()  # the fake is free to be rude
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
