"""``el-devices foldio``, against the fake transport."""

import argparse
import subprocess
import sys

import pytest

from educelab.devices.app import foldio
from educelab.devices.app.cli import main
from educelab.devices.stages import foldio360 as f
from educelab.devices.stages.foldio360 import Foldio360
from fakes.foldio360 import FIRMWARE, FakeNusTransport, fake_bleak


@pytest.fixture
def turntable(monkeypatch):
    """Make the command connect to a fake turntable, and return its transport."""
    monkeypatch.setattr(f, "_SETTLE_QUIET_S", 0.01)
    monkeypatch.setattr(f, "_CANCEL_SPACING_S", 0.0)
    transport = FakeNusTransport(angle=100)
    made = []

    def make(args):
        made.append(args)
        return Foldio360(transport=transport)

    monkeypatch.setattr(foldio, "_turntable", make)
    transport.made = made
    return transport


def test_status(turntable, capsys):
    turntable.brightness = 40
    assert main(["foldio", "status"]) == 0
    out = capsys.readouterr().out
    assert f"Foldio360 at FA:KE, firmware {FIRMWARE}" in out
    assert "position: 100°" in out
    assert "halo: 40%" in out
    assert not turntable.connected, "the command closes the connection"


def test_rotate_is_literal_and_signed(turntable, capsys):
    assert main(["foldio", "rotate", "ccw", "380", "-s", "3"]) == 0
    rotations = [c for c in turntable.sent if str(c).startswith("rotate")]
    assert rotations == ["rotate(CCW,360,3,1)", "rotate(CCW,20,3,1)"]
    assert "now at 80°" in capsys.readouterr().out


def test_goto_takes_the_short_way(turntable, capsys):
    assert main(["foldio", "goto", "90"]) == 0
    assert "rotate(CCW,10,1,1)" in turntable.sent
    assert "turned from 100° to 90°" in capsys.readouterr().out


def test_halo_sets_and_reads_back(turntable, capsys):
    assert main(["foldio", "halo", "0.25"]) == 0
    assert turntable.brightness == 25
    assert "halo at 25%" in capsys.readouterr().out


def test_stop_sends_both_cancels(turntable, capsys):
    assert main(["foldio", "stop"]) == 0
    assert turntable.sent[-3:] == ["cancel(0)", "cancel(1)", "get_position"]
    assert "stopped at 100°" in capsys.readouterr().out


def test_address_and_scan_timeout_reach_the_driver(monkeypatch):
    args = foldio_args(["--address", "AA:BB", "--scan-timeout", "3", "status"])
    table = foldio._turntable(args)
    assert table._transport.address == "AA:BB"
    assert table._transport.scan_timeout == 3.0


def test_scan_lists_advertisers(monkeypatch, capsys):
    uuid = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
    monkeypatch.setattr(f, "_bleak", fake_bleak([("AA:01", "Foldio360", (uuid,), -60)]))
    assert main(["foldio", "scan"]) == 0
    assert "AA:01" in capsys.readouterr().out


def test_empty_scan_explains_itself(monkeypatch, capsys):
    monkeypatch.setattr(f, "_bleak", fake_bleak([]))
    assert main(["foldio", "--scan-timeout", "1", "scan"]) == 1
    assert "Bluetooth permission" in capsys.readouterr().out


def test_device_errors_are_reported(monkeypatch, capsys):
    monkeypatch.setattr(f, "_bleak", fake_bleak(connect_error=OSError("no adapter")))
    assert main(["foldio", "--address", "AA:BB", "status"]) == 1
    assert "no adapter" in capsys.readouterr().err


def test_discover_lists_turntables(monkeypatch, capsys):
    uuid = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
    monkeypatch.setattr(f, "_bleak", fake_bleak([("AA:01", "Foldio360", (uuid,), -60)]))
    assert main(["discover"]) == 0
    assert "foldio360: Foldio360 at AA:01" in capsys.readouterr().out


# -- argument handling, all of which fails before anything connects -------------


def foldio_args(argv: list[str]) -> argparse.Namespace:
    from educelab.devices.app.cli import build_parser

    return build_parser().parse_args(["foldio", *argv])


def test_direction_refuses_the_apps_labels():
    for label in ("LEFT", "left", "Right"):
        with pytest.raises(argparse.ArgumentTypeError, match="inverted"):
            foldio.parse_direction(label)
    assert foldio.parse_direction("cw") == "CW"
    assert foldio.parse_direction("CCW") == "CCW"
    with pytest.raises(argparse.ArgumentTypeError):
        foldio.parse_direction("sideways")


def test_bounded_int():
    angle = foldio.bounded_int(0, 359, "angle")
    assert angle("0") == 0
    assert angle("359") == 359
    for bad in ("360", "-1", "abc"):
        with pytest.raises(argparse.ArgumentTypeError):
            angle(bad)
    degrees = foldio.bounded_int(1, label="degrees")
    with pytest.raises(argparse.ArgumentTypeError):
        degrees("0")
    assert degrees("380") == 380


def test_parses_without_connecting():
    args = foldio_args(["rotate", "ccw", "25", "-s", "3"])
    assert (args.direction, args.degrees, args.speed) == ("CCW", 25, 3)


@pytest.mark.parametrize(
    "argv",
    [
        ["rotate", "LEFT", "90"],
        ["rotate", "CW", "0"],
        ["rotate", "CW", "90", "-s", "4"],
        ["goto", "360"],
        ["halo", "1.5"],
        ["halo", "bright"],
        [],
    ],
)
def test_bad_arguments_are_usage_errors(turntable, argv, capsys):
    with pytest.raises(SystemExit) as info:
        main(["foldio", *argv])
    assert info.value.code == 2
    assert turntable.made == [], "nothing connects for a usage error"


def test_cli_starts_without_importing_the_driver():
    code = (
        "import sys; from educelab.devices.app.cli import build_parser; "
        "build_parser(); "
        "sys.exit('educelab.devices.stages.foldio360' in sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
