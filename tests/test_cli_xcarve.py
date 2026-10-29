import _thread
import threading

import pytest

from educelab.devices import _serial
from educelab.devices._serial import PortInfo, SerialTransport
from educelab.devices.app.cli import main
from educelab.devices.stages import xcarve
from fakes.grbl import SOFT_RESET, GrblMachine

XCONTROLLER = PortInfo(
    "/dev/cu.usbserial-21320",
    vid=0x0403,
    pid=0x6001,
    serial_number="XCONTROLLER3HPX2Z",
)


@pytest.fixture
def machine(monkeypatch):
    machine = GrblMachine()
    monkeypatch.setattr(xcarve, "BANNER_TIMEOUT_S", 0.2)
    monkeypatch.setattr(_serial, "list_ports", lambda: [XCONTROLLER])
    monkeypatch.setattr(
        xcarve,
        "_default_transport",
        lambda port: SerialTransport(
            port, baudrate=115200, timeout=0.5, opener=machine.open
        ),
    )
    return machine


def jogs(machine):
    return [line for line in machine.lines if line.startswith("$J")]


def test_status_reports_without_moving(machine, capsys):
    assert main(["xcarve", "status"]) == 0
    out = capsys.readouterr().out
    assert "X-Carve at /dev/cu.usbserial-21320" in out
    assert "GRBL 1.1g" in out
    assert "speed 10 mm/s, at most 166.667 mm/s" in out
    assert "position x=0, y=0, z=0" in out
    assert machine.lines == ["$$"]


def test_home_homes(machine, capsys):
    assert main(["xcarve", "home"]) == 0
    assert "homed at x=-1, y=-1, z=-1" in capsys.readouterr().out
    assert machine.lines[-1] == "$H"
    assert jogs(machine) == []


def test_move_homes_first_then_moves_slowly(machine, capsys):
    assert main(["xcarve", "move", "--x", "-50", "--z", "-10"]) == 0
    assert machine.lines[1] == "$H"
    assert jogs(machine) == ["$J=G53 G21 X-50.000 Z-10.000 F600"]
    assert "at x=-50, y=-1, z=-10" in capsys.readouterr().out


def test_move_outside_the_limits_fails_before_homing(machine, capsys):
    assert main(["xcarve", "move", "--x", "10"]) == 1
    assert "outside" in capsys.readouterr().err
    assert "$H" not in machine.lines


def test_move_needs_a_target(machine):
    with pytest.raises(SystemExit, match="--x"):
        main(["xcarve", "move"])


def test_limits_and_speed_can_be_changed(machine, capsys):
    argv = ["xcarve", "--speed", "5", "--x-limits", "-100", "0"]
    assert main([*argv, "move", "--x", "-80"]) == 0
    assert jogs(machine) == ["$J=G53 G21 X-80.000 F300"]
    assert main([*argv, "move", "--x", "-200"]) == 1


def test_check_moves_each_axis_toward_the_middle_and_back(machine, capsys):
    assert main(["xcarve", "check", "--distance", "5"]) == 0
    assert jogs(machine) == [
        "$J=G53 G21 X-6.000 F600",
        "$J=G53 G21 X-1.000 F600",
        "$J=G53 G21 Y-6.000 F600",
        "$J=G53 G21 Y-1.000 F600",
        "$J=G53 G21 Z-6.000 F600",
        "$J=G53 G21 Z-1.000 F600",
    ]
    assert capsys.readouterr().out.count(": ok") == 6


def test_ctrl_c_during_homing_stops_homing(machine, capsys):
    machine.homing_s = 30.0
    threading.Timer(0.3, _thread.interrupt_main).start()
    assert main(["xcarve", "home"]) == 130
    assert "stopped" in capsys.readouterr().out
    assert SOFT_RESET in machine.realtime
    assert machine.state != "Home"


@pytest.mark.parametrize(
    ("speed", "message"), [("200", "above the controller's fastest"), ("0", "at least")]
)
def test_a_bad_speed_is_reported_without_a_traceback(machine, speed, message):
    with pytest.raises(SystemExit, match=message):
        main(["xcarve", "--speed", speed, "status"])


def test_missing_controller_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(_serial, "list_ports", list)
    assert main(["xcarve", "status"]) == 1
    assert "no X-Controller" in capsys.readouterr().err


def test_ctrl_c_stops_the_machine(machine, monkeypatch, capsys):
    def interrupted(self, targets, timeout=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(xcarve.XCarve, "move_to", interrupted)
    assert main(["xcarve", "move", "--x", "-50"]) == 130
    assert "stopped" in capsys.readouterr().out
    assert machine.realtime[-2:] == [0x85, 0x85]
