import pytest

from educelab.devices._serial import SerialTransport
from educelab.devices.app.cli import main
from educelab.devices.lights import xlc
from fakes.serial_port import ScriptedSerial
from fakes.xlc import XLCFirmware


@pytest.fixture
def board(monkeypatch):
    firmware = XLCFirmware()
    fake = ScriptedSerial(respond=firmware)
    monkeypatch.setattr(xlc, "HANDSHAKE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        xlc,
        "_default_transport",
        lambda port: SerialTransport(
            port, baudrate=9600, timeout=0.05, opener=fake.open
        ),
    )
    return fake, firmware


def test_lights_each_channel_then_turns_off(board, capsys):
    fake, firmware = board
    assert main(["xlc", "--port", "/dev/cu.usbmodem00001", "--dwell", "0"]) == 0
    out = capsys.readouterr().out
    assert "XLC2 at /dev/cu.usbmodem00001, firmware 2.0.0, hardware 1.0" in out
    assert "white at 25%" in out and "940nm at 25%" in out
    lit = [line for line in fake.written if line.endswith(",64>")]
    assert lit == ["<B,64>", "<X,64>"]
    assert (firmware.pixels[0], firmware.xl4) == (0, 0)


def test_needs_a_port_or_serial_number(capsys):
    with pytest.raises(SystemExit) as info:
        main(["xlc"])
    assert info.value.code == 2


def test_old_firmware_is_reported(board, capsys):
    _, firmware = board
    firmware.identity = "EduceLab XLC2 (FW 1.2.0)"
    assert main(["xlc", "--port", "COM5", "--dwell", "0"]) == 1
    assert "firmware/xlc2" in capsys.readouterr().err


@pytest.mark.parametrize("level", ["0", "1.5", "x"])
def test_level_must_be_a_fraction_above_zero(level, capsys):
    with pytest.raises(SystemExit) as info:
        main(["xlc", "--port", "COM5", "--level", level])
    assert info.value.code == 2
