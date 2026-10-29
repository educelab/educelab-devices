import time

import pytest

from educelab.devices._serial import SerialTransport
from fakes.grbl import BANNER, LOCKED, GrblMachine


def connect(machine):
    transport = SerialTransport(
        "/dev/ttyGRBL", baudrate=115200, timeout=0.5, opener=machine.open
    )
    transport.open()
    return transport


def status(transport):
    transport.write_bytes(b"?")
    return transport.read_line()


def homed(**kwargs):
    machine = GrblMachine(**kwargs)
    transport = connect(machine)
    transport.read_line(), transport.read_line()
    transport.write_line("$H")
    assert transport.read_line() == "ok"
    return machine, transport


def test_opening_resets_into_an_alarm_lock():
    transport = connect(GrblMachine())
    assert transport.read_line() == BANNER
    assert transport.read_line() == LOCKED
    assert status(transport) == "<Alarm|MPos:0.000,0.000,0.000|FS:0,0>"


def test_locked_machine_refuses_jogs_but_lists_settings():
    transport = connect(GrblMachine())
    transport.read_line(), transport.read_line()
    transport.write_line("$J=G53 G21 X-10 F1000")
    assert transport.read_line() == "error:9"
    transport.write_line("$$")
    lines = []
    while (line := transport.read_line()) != "ok":
        lines.append(line)
    assert "$110=10000.000" in lines
    assert "$112=750.000" in lines


def test_without_a_reset_on_open_only_a_soft_reset_sends_the_banner():
    machine = GrblMachine(banner_on_open=False)
    transport = connect(machine)
    with pytest.raises(Exception, match="no reply"):
        transport.read_line(timeout=0.05)
    transport.write_bytes(b"\x18")
    assert transport.read_line() == BANNER


def test_homing_replies_ok_when_done_and_sets_the_home_position():
    machine, transport = homed(home=(-1.0, -1.0, -1.0))
    assert status(transport) == "<Idle|MPos:-1.000,-1.000,-1.000|FS:0,0>"
    assert machine.lines == ["$H"]


def test_jog_runs_at_its_feed_rate_then_goes_idle():
    _, transport = homed(speedup=100.0)
    # 30 mm at 6000 mm/min is 0.3 s, or 3 ms at 100x.
    transport.write_line("$J=G53 G21 X-31 F6000")
    assert transport.read_line() == "ok"
    assert status(transport).startswith("<Jog|")
    time.sleep(0.02)
    assert status(transport) == "<Idle|MPos:-31.000,-1.000,-1.000|FS:0,0>"


def test_z_is_capped_at_its_own_maximum_rate():
    machine, transport = homed(speedup=100.0)
    # 10 mm of Z at 750 mm/min is 0.8 s, or 8 ms at 100x, whatever F says.
    transport.write_line("$J=G53 G21 Z-11 F10000")
    transport.read_line()
    time.sleep(0.004)
    assert machine.moving
    time.sleep(0.01)
    assert not machine.moving


def test_jog_cancel_stops_partway():
    machine, transport = homed(speedup=1.0)
    transport.write_line("$J=G53 G21 X-500 F600")
    transport.read_line()
    time.sleep(0.05)
    transport.write_bytes(b"\x85")
    x = machine.current_position()[0]
    assert -500 < x < -1
    assert status(transport).startswith("<Idle|")
    time.sleep(0.02)
    assert machine.current_position()[0] == x


def test_relative_jogs_queue_up():
    machine, transport = homed()
    transport.write_line("$J=G91 G21 X-5 F6000")
    transport.write_line("$J=G91 G21 X-5 F6000")
    assert [transport.read_line(), transport.read_line()] == ["ok", "ok"]
    time.sleep(0.01)
    assert machine.current_position() == (-11.0, -1.0, -1.0)


def test_jog_needs_a_feed_rate_and_soft_limits_reject_far_targets():
    _, transport = homed(settings={20: 1.0})
    transport.write_line("$J=G53 G21 X-10")
    assert transport.read_line() == "error:22"
    transport.write_line("$J=G53 G21 X-800 F1000")
    assert transport.read_line() == "error:15"


def test_alarm_stops_motion_and_locks():
    machine, transport = homed(speedup=1.0)
    transport.write_line("$J=G53 G21 X-500 F600")
    transport.read_line()
    machine.alarm(1)
    assert transport.read_line() == "ALARM:1"
    assert status(transport).startswith("<Alarm|")
    transport.write_line("$J=G53 G21 X-10 F1000")
    assert transport.read_line() == "error:9"


def test_unplugging_fails_reads():
    machine, transport = homed()
    machine.unplug()
    with pytest.raises(Exception, match="lost serial port"):
        transport.read_line()
