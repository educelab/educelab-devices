import pytest

from educelab.devices import _serial
from educelab.devices._serial import PortInfo, SerialTransport
from educelab.devices.app.cli import main
from educelab.devices.lights import xlc
from fakes.serial_port import ScriptedSerial
from fakes.xlc import XLCFirmware

HOLTEK = PortInfo("/dev/cu.usbmodem00001", vid=0x04D9, pid=0xB534)
CH340 = PortInfo("/dev/cu.usbserial-110", vid=0x1A86, pid=0x7523)


@pytest.fixture
def attach(monkeypatch):
    """Attach boards, given as port → firmware; returns port → ScriptedSerial."""
    monkeypatch.setattr(xlc, "HANDSHAKE_TIMEOUT_S", 0.05)

    def attach(boards):
        fakes = {port.device: ScriptedSerial(respond=fw) for port, fw in boards.items()}
        monkeypatch.setattr(_serial, "list_ports", lambda: list(boards))
        monkeypatch.setattr(
            xlc,
            "_default_transport",
            lambda port: SerialTransport(
                port, baudrate=9600, timeout=0.05, opener=fakes[port].open
            ),
        )
        return fakes

    return attach


@pytest.fixture
def board(attach):
    firmware = XLCFirmware(unit_id="XLC2-01")
    fake = attach({HOLTEK: firmware})[HOLTEK.device]
    return fake, firmware


def test_lights_each_channel_then_turns_off(board, capsys):
    fake, firmware = board
    assert main(["xlc", "--port", HOLTEK.device, "--dwell", "0"]) == 0
    out = capsys.readouterr().out
    assert f"XLC2 XLC2-01 at {HOLTEK.device}, firmware 2.1.0, hardware 1.0" in out
    assert "white at 25%" in out and "940nm at 25%" in out
    lit = [line for line in fake.written if line.endswith(",64>")]
    assert lit == ["<B,64>", "<X,64>"]
    assert (firmware.pixels[0], firmware.xl4) == (0, 0)


def test_a_board_without_an_id_says_so(attach, capsys):
    attach({HOLTEK: XLCFirmware()})
    assert main(["xlc", "--port", HOLTEK.device, "--dwell", "0"]) == 0
    assert f"XLC2 (no ID) at {HOLTEK.device}" in capsys.readouterr().out


def test_finds_the_only_board_by_probing(board, capsys):
    assert main(["xlc", "--dwell", "0"]) == 0
    assert f"XLC2 XLC2-01 at {HOLTEK.device}" in capsys.readouterr().out


def test_finds_a_board_by_unit_id(attach, capsys):
    attach({HOLTEK: XLCFirmware(unit_id="XLC2-01"), CH340: XLCFirmware(unit_id="B")})
    assert main(["xlc", "--serial-number", "B", "--dwell", "0"]) == 0
    assert f"XLC2 B at {CH340.device}" in capsys.readouterr().out


def test_several_boards_are_listed_instead_of_picked(attach, capsys):
    fakes = attach({HOLTEK: XLCFirmware(unit_id="XLC2-01"), CH340: XLCFirmware()})
    assert main(["xlc", "--dwell", "0"]) == 1
    err = capsys.readouterr().err
    assert f"XLC2-01 at {HOLTEK.device}" in err
    assert f"(no ID) at {CH340.device}" in err
    assert "--port" in err
    assert not any(line.endswith(",64>") for f in fakes.values() for line in f.written)


def test_no_board_found(attach, capsys):
    attach({})
    assert main(["xlc"]) == 1
    assert "no XLC2" in capsys.readouterr().err


def test_no_board_found_mentions_ports_in_use(attach, monkeypatch, capsys):
    # A port another program holds is skipped, so its board doesn't answer.
    attach({HOLTEK: XLCFirmware()})
    monkeypatch.setattr(
        xlc,
        "_default_transport",
        lambda port: SerialTransport(
            port, baudrate=9600, opener=ScriptedSerial(present=False).open
        ),
    )
    assert main(["xlc"]) == 1
    assert "another program" in capsys.readouterr().err


def test_set_id_stores_it_without_lighting(board, capsys):
    fake, firmware = board
    assert main(["xlc", "--set-id", "XLC2-09"]) == 0
    assert firmware.unit_id == "XLC2-09"
    assert "XLC2-01" in capsys.readouterr().out
    assert not any(line.endswith(",64>") for line in fake.written)


def test_set_id_refuses_when_several_boards_are_attached(attach, capsys):
    fakes = attach({HOLTEK: XLCFirmware(), CH340: XLCFirmware()})
    assert main(["xlc", "--set-id", "XLC2-09"]) == 1
    assert not any(line.startswith("<U") for f in fakes.values() for line in f.written)


@pytest.mark.parametrize("option", [["--level", "0.5"], ["--dwell", "1"]])
def test_set_id_refuses_lighting_options(board, capsys, option):
    _, firmware = board
    assert main(["xlc", "--set-id", "XLC2-09", *option]) == 2
    assert "--set-id" in capsys.readouterr().err
    assert firmware.unit_id == "XLC2-01"


def test_no_board_found_mentions_permissions(attach, capsys):
    attach({})
    assert main(["xlc"]) == 1
    assert "permission" in capsys.readouterr().err


def test_set_id_rejects_an_invalid_id(board, capsys):
    with pytest.raises(SystemExit) as info:
        main(["xlc", "--set-id", "not valid"])
    assert info.value.code == 2


def test_old_firmware_is_reported(board, capsys):
    _, firmware = board
    firmware.identity = "EduceLab XLC2 (FW 1.2.0)"
    assert main(["xlc", "--port", HOLTEK.device, "--dwell", "0"]) == 1
    assert "firmware/xlc2" in capsys.readouterr().err


@pytest.mark.parametrize("level", ["0", "1.5", "x"])
def test_level_must_be_a_fraction_above_zero(level, capsys):
    with pytest.raises(SystemExit) as info:
        main(["xlc", "--port", "COM5", "--level", level])
    assert info.value.code == 2
