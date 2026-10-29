import threading
import time

import pytest

from contracts import StageContract
from educelab.devices._serial import PortInfo, SerialTransport
from educelab.devices.device import DeviceInfo, Disconnected
from educelab.devices.errors import (
    CommandRejected,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    NotConnected,
    PositionError,
)
from educelab.devices.stages import xcarve
from educelab.devices.stages.xcarve import GrblAlarm, XCarve
from fakes.grbl import SOFT_RESET, GrblMachine
from fakes.serial_port import ScriptedSerial

# pgs-scan's machine bounds for the X-Carve, in machine coordinates.
LIMITS = {"x": (-699.0, 0.0), "y": (-699.0, 0.0), "z": (-45.0, -1.0)}


@pytest.fixture(autouse=True)
def quick_banner(monkeypatch):
    monkeypatch.setattr(xcarve, "BANNER_TIMEOUT_S", 0.2)


def make_transport(machine, port="/dev/ttyGRBL"):
    return SerialTransport(port, baudrate=115200, timeout=0.5, opener=machine.open)


def make_stage(machine, port="/dev/ttyGRBL", speed=50.0, limits=LIMITS):
    return XCarve(
        port, limits=limits, speed=speed, transport=make_transport(machine, port)
    )


@pytest.fixture
def machine():
    return GrblMachine()


class TestXCarveContract(StageContract):
    @pytest.fixture(autouse=True)
    def no_ports(self, monkeypatch):
        monkeypatch.setattr(xcarve, "list_ports", list)

    @pytest.fixture
    def make_device(self):
        return lambda: make_stage(GrblMachine())

    @pytest.fixture
    def position_tolerance(self):
        return xcarve.ARRIVAL_TOLERANCE_MM


@pytest.fixture
def stage(machine):
    stage = make_stage(machine)
    stage.open()
    yield stage
    stage.close()


# Opening


def test_open_reads_the_banner_and_settings_without_homing(machine, stage):
    assert stage.info.firmware_version == "1.1g"
    assert stage.info.model == "X-Carve"
    assert not stage.is_homed
    assert "$H" not in machine.lines
    assert machine.lines == ["$$"]


def test_open_reads_the_position(stage):
    assert stage.position() == {"x": 0.0, "y": 0.0, "z": 0.0}


def test_open_soft_resets_a_board_that_did_not_reset(monkeypatch):
    machine = GrblMachine(banner_on_open=False)
    with make_stage(machine) as stage:
        assert stage.info.firmware_version == "1.1g"
    assert SOFT_RESET in machine.realtime


def test_open_fails_unless_grbl_answers():
    silent = ScriptedSerial(respond=lambda line: [])
    stage = XCarve(
        "/dev/ttyGRBL",
        limits=LIMITS,
        speed=50.0,
        transport=SerialTransport(
            "/dev/ttyGRBL", baudrate=115200, timeout=0.05, opener=silent.open
        ),
    )
    with pytest.raises(DeviceNotFound, match="ttyGRBL"):
        stage.open()
    assert not silent.is_open
    assert not stage.is_open


def test_open_fails_when_the_port_is_missing():
    with pytest.raises(DeviceNotFound):
        make_stage(GrblMachine(present=False)).open()


def test_speed_above_the_fastest_axis_fails_open(machine):
    stage = make_stage(machine, speed=200.0)
    with pytest.raises(ValueError, match="166.667 mm/s"):
        stage.open()
    assert not machine.is_open


def test_default_transport_uses_grbl_port_settings():
    stage = XCarve("COM4", limits=LIMITS, speed=50.0)
    assert (stage._transport.port, stage._transport.baudrate) == (
        "COM4",
        xcarve.BAUD_RATE,
    )


@pytest.mark.parametrize(
    "limits",
    [
        {"x": (-699.0, 0.0), "y": (-699.0, 0.0)},
        {**LIMITS, "a": (0.0, 1.0)},
        {**LIMITS, "z": (0.0, -45.0)},
    ],
    ids=["missing z", "extra axis", "inverted"],
)
def test_limits_must_give_each_axis_a_range(limits):
    with pytest.raises(ValueError):
        XCarve("COM4", limits=limits, speed=50.0)


@pytest.mark.parametrize("speed", [0.0, -5.0, float("nan")])
def test_speed_must_be_positive(speed):
    with pytest.raises(ValueError):
        XCarve("COM4", limits=LIMITS, speed=speed)


def test_axes_are_linear_in_mm_with_the_given_limits():
    stage = XCarve("COM4", limits=LIMITS, speed=50.0)
    assert [(a.name, a.unit, a.minimum, a.maximum) for a in stage.axes] == [
        ("x", "mm", -699.0, 0.0),
        ("y", "mm", -699.0, 0.0),
        ("z", "mm", -45.0, -1.0),
    ]


# Homing


def test_moves_before_homing_raise_without_sending_anything(machine, stage):
    with pytest.raises(PositionError, match="homed"):
        stage.move_to({"x": -10.0})
    with pytest.raises(PositionError, match="homed"):
        stage.jog("x", 1)
    assert not any(line.startswith("$J") for line in machine.lines)


def test_home_runs_homing_and_reads_the_home_position(machine, stage):
    stage.home()
    assert stage.is_homed
    assert machine.lines[-1] == "$H"
    assert stage.position() == {"x": -1.0, "y": -1.0, "z": -1.0}


def test_homing_that_overruns_its_timeout_is_aborted():
    machine = GrblMachine(homing_s=30.0)
    with make_stage(machine) as stage:
        with pytest.raises(DeviceTimeout):
            stage.home(timeout=0.1)
        assert not stage.is_homed
        assert SOFT_RESET in machine.realtime


def test_homing_alarm_raises_and_leaves_the_stage_unhomed():
    machine = GrblMachine(homing_s=30.0)
    with make_stage(machine) as stage:
        threading.Timer(0.05, machine.alarm, args=(9,)).start()
        with pytest.raises(DeviceError, match="could not find the limit switch"):
            stage.home(timeout=5.0)
        assert not stage.is_homed


def test_homing_rejected_by_grbl_raises_command_rejected():
    machine = GrblMachine(settings={22: 0.0})
    with make_stage(machine) as stage:
        with pytest.raises(CommandRejected, match="homing is not enabled"):
            stage.home()
        assert not stage.is_homed


def test_reopening_forgets_homing(machine, stage):
    stage.home()
    stage.close()
    stage.open()
    assert not stage.is_homed


# Closing


def test_close_cancels_any_jog_and_stops_reading(machine, stage):
    stage.close()
    assert machine.realtime[-1] == xcarve.JOG_CANCEL[0]
    assert not machine.is_open
    assert stage._reader is None


# Discovery and finding a controller without a port

XCONTROLLER = PortInfo(
    "/dev/cu.usbserial-21320",
    vid=0x0403,
    pid=0x6001,
    serial_number="XCONTROLLER3HPX2Z",
    description="X-Controller",
)
OTHER_XCONTROLLER = PortInfo(
    "/dev/cu.usbserial-21330", vid=0x0403, pid=0x6001, serial_number="XCONTROLLERAB"
)
OTHER_FTDI = PortInfo(
    "/dev/cu.usbserial-A1", vid=0x0403, pid=0x6001, serial_number="A10K4XYZ"
)
XLC2 = PortInfo("/dev/cu.usbmodem00001", vid=0x04D9, pid=0xB534, serial_number="0000")


def attach(monkeypatch, boards):
    """Attach ``boards``, a mapping of PortInfo to the fake port behind it."""
    by_device = {port.device: fake for port, fake in boards.items()}
    monkeypatch.setattr(xcarve, "list_ports", lambda: list(boards))
    monkeypatch.setattr(
        xcarve,
        "_default_transport",
        lambda port: make_transport(by_device[port], port),
    )
    return boards


def test_discover_lists_x_controllers_without_opening_anything(monkeypatch):
    boards = attach(
        monkeypatch,
        {
            XCONTROLLER: GrblMachine(),
            OTHER_FTDI: GrblMachine(),
            XLC2: GrblMachine(),
        },
    )
    assert XCarve.discover() == [
        DeviceInfo("xcarve", "X-Carve", XCONTROLLER.device, "XCONTROLLER3HPX2Z")
    ]
    assert all(fake.opened_with is None for fake in boards.values())


def test_open_without_a_port_uses_the_only_x_controller(monkeypatch):
    boards = attach(
        monkeypatch, {OTHER_FTDI: GrblMachine(), XCONTROLLER: GrblMachine()}
    )
    with XCarve(limits=LIMITS, speed=50.0) as stage:
        assert stage.info.address == XCONTROLLER.device
        assert stage.info.serial_number == "XCONTROLLER3HPX2Z"
    assert boards[OTHER_FTDI].opened_with is None


def test_open_without_a_port_names_the_choices_when_several_match(monkeypatch):
    attach(monkeypatch, {XCONTROLLER: GrblMachine(), OTHER_XCONTROLLER: GrblMachine()})
    with pytest.raises(DeviceError, match="XCONTROLLER3HPX2Z.*serial_number="):
        XCarve(limits=LIMITS, speed=50.0).open()


def test_open_without_a_port_fails_when_none_is_attached(monkeypatch):
    attach(monkeypatch, {OTHER_FTDI: GrblMachine()})
    with pytest.raises(DeviceNotFound, match="port="):
        XCarve(limits=LIMITS, speed=50.0).open()


def test_serial_number_picks_a_controller(monkeypatch):
    attach(
        monkeypatch,
        {XCONTROLLER: GrblMachine(), OTHER_XCONTROLLER: GrblMachine()},
    )
    with XCarve(limits=LIMITS, speed=50.0, serial_number="xcontrollerab") as stage:
        assert stage.info.address == OTHER_XCONTROLLER.device


def test_port_and_serial_number_are_exclusive():
    with pytest.raises(ValueError):
        XCarve("COM4", limits=LIMITS, speed=50.0, serial_number="XCONTROLLERAB")


# Moving


@pytest.fixture
def homed(stage):
    stage.home()
    return stage


def jogs(machine):
    return [line for line in machine.lines if line.startswith("$J")]


@pytest.mark.parametrize(
    "target", [{"x": 1.0}, {"y": -700.0}, {"z": 0.0}, {"z": -46.0}]
)
def test_targets_outside_the_limits_raise_and_are_never_clamped(machine, homed, target):
    # pgs-scan #71: XController clamped the target, then waited forever for
    # the stage to reach the unclamped one.
    with pytest.raises(PositionError, match="outside"):
        homed.move_to(target)
    assert jogs(machine) == []


def test_move_by_past_a_limit_raises(machine, homed):
    with pytest.raises(PositionError):
        homed.move_by({"x": 2.0})
    assert jogs(machine) == []


def test_move_to_jogs_in_machine_coordinates_and_waits_to_arrive(machine, homed):
    homed.move_to({"x": -100.0, "z": -20.0})
    assert jogs(machine) == ["$J=G53 G21 X-100.000 Z-20.000 F3000"]
    assert not machine.moving
    assert machine.current_position() == (-100.0, -1.0, -20.0)
    assert homed.position() == {"x": -100.0, "y": -1.0, "z": -20.0}


def test_move_by_moves_from_the_current_position(homed):
    homed.move_by({"y": -10.5})
    assert homed.position()["y"] == pytest.approx(-11.5)


def test_moves_use_the_current_speed(machine, homed):
    homed.set_speed(10.0)
    homed.move_to({"x": -5.0})
    assert jogs(machine)[-1].endswith(" F600")


def test_a_jog_grbl_rejects_raises_command_rejected():
    machine = GrblMachine(settings={20: 1.0, 130: 100.0})
    with make_stage(machine) as stage:
        stage.home()
        with pytest.raises(CommandRejected, match="exceeds machine travel"):
            stage.move_to({"x": -200.0})


def test_a_move_that_overruns_its_timeout_is_cancelled():
    # pgs-scan #67: XController's blocking move had no timeout at all.
    machine = GrblMachine(speedup=1.0)
    with make_stage(machine) as stage:
        stage.home()
        with pytest.raises(DeviceTimeout):
            stage.move_to({"x": -600.0}, timeout=0.3)
        assert machine.realtime[-1] == xcarve.JOG_CANCEL[0]
        assert not machine.moving
        assert -600.0 < stage.position()["x"] < -1.0


def test_a_move_that_stops_short_raises_position_error():
    machine = GrblMachine(speedup=1.0)
    with make_stage(machine) as stage:
        stage.home()
        # Something other than this driver cancels the jog partway.
        threading.Timer(0.6, machine.write, args=(b"\x85",)).start()
        with pytest.raises(PositionError, match="short"):
            stage.move_to({"x": -600.0}, timeout=10.0)


def test_default_move_limit_follows_distance_and_speed(homed):
    start = {"x": -1.0, "y": -1.0, "z": -1.0}
    # 100 mm of X at 50 mm/s is 2 s: doubled, plus the margin.
    assert homed._move_limit(start, {**start, "x": -101.0}) == pytest.approx(
        2 * 2.0 + xcarve.MOVE_MARGIN_S
    )
    # Z is held to its own 750 mm/min (12.5 mm/s), whatever the speed.
    assert homed._move_limit(start, {**start, "z": -11.0}) == pytest.approx(
        2 * 0.8 + xcarve.MOVE_MARGIN_S
    )
    # A long slow move gets longer than Stage.move_timeout's 60 s.
    homed.set_speed(1.0)
    assert homed._move_limit(start, {**start, "x": -101.0}) > homed.move_timeout


# Jogging, stopping, and live position


@pytest.fixture
def real_time():
    """A homed stage on a machine that moves at its real speed."""
    machine = GrblMachine(speedup=1.0)
    stage = make_stage(machine)
    stage.open()
    stage.home()
    yield stage, machine
    stage.close()


def in_background(call, *args):
    """Run ``call`` on a thread; returns the thread and what it raised."""
    raised = []

    def run():
        try:
            call(*args)
        except DeviceError as exc:
            raised.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, raised


@pytest.mark.parametrize(
    ("direction", "expected"),
    [(1, "$J=G53 G21 X0.000 F3000"), (-1, "$J=G53 G21 X-699.000 F3000")],
)
def test_jog_heads_for_the_axis_limit_and_returns(homed, machine, direction, expected):
    homed.set_speed(50.0)
    homed.jog("x", direction)
    assert jogs(machine)[-1] == expected


def test_stop_ends_a_jog_short_of_the_limit(real_time):
    stage, machine = real_time
    stage.jog("x", -1)
    assert machine.moving
    time.sleep(0.1)
    stage.stop()
    assert machine.realtime[-1] == xcarve.JOG_CANCEL[0]
    assert not machine.moving
    assert -699.0 < stage.position()["x"] < -1.0


def test_stop_interrupts_a_move_running_on_another_thread(real_time):
    stage, machine = real_time
    thread, raised = in_background(stage.move_to, {"x": -600.0})
    time.sleep(0.3)
    stage.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert isinstance(raised[0], PositionError)
    assert "stop()" in str(raised[0])
    assert not machine.moving


def test_a_move_after_stop_runs_normally(real_time):
    stage, _ = real_time
    stage.stop()
    stage.move_to({"x": -5.0})
    assert stage.position()["x"] == pytest.approx(-5.0)


def test_stop_during_homing_resets_the_controller():
    machine = GrblMachine(homing_s=30.0)
    with make_stage(machine) as stage:
        thread, raised = in_background(stage.home)
        time.sleep(0.2)
        stage.stop()
        thread.join(timeout=5)
        assert isinstance(raised[0], PositionError)
        assert machine.realtime[-1] == SOFT_RESET
        assert not stage.is_homed


def test_position_is_live_during_a_move(real_time):
    stage, _ = real_time
    thread, _ = in_background(stage.move_to, {"x": -100.0})
    seen = []
    while thread.is_alive():
        seen.append(stage.position()["x"])
        time.sleep(0.1)
    assert any(-100.0 < x < -1.0 for x in seen)
    assert seen == sorted(seen, reverse=True)


def test_position_times_out_without_a_status_report(machine, stage):
    machine.mute_status = True
    time.sleep(xcarve.STATUS_INTERVAL_S)
    with pytest.raises(DeviceTimeout):
        stage.position()


# Alarms, resets, and lost connections


def test_alarm_during_a_move_raises_and_reaches_listeners(real_time):
    stage, machine = real_time
    events = []
    stage.add_listener(events.append)
    threading.Timer(0.3, machine.alarm, args=(1,)).start()
    with pytest.raises(DeviceError, match="hard limit"):
        stage.move_to({"x": -600.0})
    assert [(type(e), e.code, e.meaning) for e in events] == [
        (GrblAlarm, 1, "hard limit triggered")
    ]
    assert not stage.is_homed
    with pytest.raises(PositionError, match="homed"):
        stage.move_to({"x": -5.0})


def test_alarm_while_idle_loses_homing(homed, machine):
    events = []
    homed.add_listener(events.append)
    machine.alarm(2)
    time.sleep(0.3)
    assert [e.code for e in events] == [2]
    assert not homed.is_homed
    homed.home()
    homed.move_to({"x": -5.0})


def test_unexpected_reset_loses_homing(homed, machine, caplog):
    machine.write(bytes([SOFT_RESET]))
    time.sleep(0.3)
    assert not homed.is_homed
    assert "reset unexpectedly" in caplog.text


def test_unplugging_reaches_listeners_and_later_calls_raise(homed, machine):
    events = []
    homed.add_listener(events.append)
    machine.unplug()
    time.sleep(0.3)
    assert [type(e) for e in events] == [Disconnected]
    with pytest.raises(NotConnected):
        homed.position()
    with pytest.raises((NotConnected, PositionError)):
        homed.move_to({"x": -5.0})
    homed.close()
    assert not homed.is_open
