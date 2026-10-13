import pytest

from educelab.devices._serial import PortInfo, SerialTransport
from educelab.devices.app.cli import main
from educelab.devices.lights import misha
from fakes.misha import MishaFirmware
from fakes.serial_port import ScriptedSerial

PANEL = PortInfo(
    "/dev/cu.usbserial-MISHA_e6a00ad8",
    vid=0x10C4,
    pid=0xEA60,
    serial_number="MISHA-e6a00ad8",
)


@pytest.fixture
def panel(monkeypatch):
    firmware = MishaFirmware()
    fake = ScriptedSerial(respond=firmware)
    monkeypatch.setattr(misha, "HANDSHAKE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(misha, "list_ports", lambda: [PANEL])
    monkeypatch.setattr(
        misha,
        "_default_transport",
        lambda port: SerialTransport(
            port, baudrate=9600, timeout=0.05, opener=fake.open
        ),
    )
    return fake, firmware


def test_steps_through_every_band_and_turns_off(panel, capsys):
    fake, firmware = panel
    assert main(["misha", "--dwell", "0", "--level", "0.25"]) == 0
    out = capsys.readouterr().out
    assert "MISHA-e6a00ad8" in out
    assert "firmware 2.0.0" in out
    assert "365nm" in out and "940nm" in out
    lit = [line for line in fake.written if line.endswith(",25")]
    assert lit == [f"{nm},25" for nm in misha.WAVELENGTHS_NM]
    assert fake.written[-1] == "OFF"
    assert firmware.lit == (0, 0)


def test_reports_what_was_lit_before(panel, capsys):
    _, firmware = panel
    firmware.lit = (450, 50)
    main(["misha", "--dwell", "0"])
    assert "450nm at 50%" in capsys.readouterr().out


def test_selects_a_panel_by_serial_number(panel, capsys):
    assert main(["misha", "--dwell", "0", "--serial-number", "misha-e6a00ad8"]) == 0


def test_port_and_serial_number_are_exclusive(capsys):
    with pytest.raises(SystemExit) as info:
        main(["misha", "--port", "COM3", "--serial-number", "MISHA-e6a00ad8"])
    assert info.value.code == 2


def test_readback_mismatch_fails(panel, capsys):
    _, firmware = panel
    real = firmware.__call__

    def stuck(line):
        replies = real(line)
        if line == "STATE":
            return ["OK 0,0"]
        return replies

    panel[0].respond = stuck
    assert main(["misha", "--dwell", "0"]) == 1
    assert "read back" in capsys.readouterr().out


def test_missing_panel_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(misha, "list_ports", list)
    assert main(["misha", "--dwell", "0"]) == 1
    assert "no named MISHA" in capsys.readouterr().err


@pytest.mark.parametrize("level", ["0", "1.5", "x"])
def test_level_must_be_a_fraction_above_zero(level, capsys):
    with pytest.raises(SystemExit) as info:
        main(["misha", "--level", level])
    assert info.value.code == 2
