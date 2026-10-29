import threading

import pytest

from educelab.devices._serial import PortInfo, SerialTransport
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import (
    CommandRejected,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    PositionError,
)
from educelab.devices.stages import xcarve
from educelab.devices.stages.xcarve import XCarve
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
