import sys
import time

import pytest

import fakes.brainstem
from educelab.devices.app import discover
from educelab.devices.app.cli import main
from educelab.devices.hubs import acroname
from fakes.brainstem import FakeBrainstem, FakeUnit

SERIAL = 0x1234ABCD
OTHER_SERIAL = 0x0BADF00D


@pytest.fixture
def sdk(monkeypatch):
    sdk = FakeBrainstem(FakeUnit(SERIAL, enumeration_delay_ms=300))
    monkeypatch.setattr(acroname, "load_sdk", lambda: sdk)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    return sdk


def test_list_shows_every_hub(sdk, capsys):
    sdk.units.append(FakeUnit(OTHER_SERIAL))
    assert main(["hub", "list"]) == 0
    out = capsys.readouterr().out
    assert "0x1234ABCD" in out and "0x0BADF00D" in out


def test_list_with_no_hubs(sdk, capsys):
    sdk.units.clear()
    assert main(["hub", "list"]) == 0
    assert "No USBHub3p" in capsys.readouterr().out


def test_status_shows_identity_delay_and_ports(sdk, capsys):
    sdk.units[0].port_state[6] = 0
    assert main(["hub", "status"]) == 0
    out = capsys.readouterr().out
    assert "0x1234ABCD" in out
    assert "firmware 2.12.0" in out
    assert "enumeration delay 0.3 s" in out
    assert "port 5: on" in out
    assert "port 6: off" in out


def test_status_is_the_default(sdk, capsys):
    assert main(["hub"]) == 0
    assert "port 0: on" in capsys.readouterr().out


def test_cycle_power_cycles_one_port(sdk, capsys):
    unit = sdk.units[0]
    assert main(["hub", "cycle", "3", "--off-time", "0"]) == 0
    assert [c for c in unit.calls if c[0].startswith("setPort")] == [
        ("setPortDisable", (3,)),
        ("setPortEnable", (3,)),
    ]
    assert "port 3: on" in capsys.readouterr().out


def test_cycle_unknown_port_fails(sdk, capsys):
    assert main(["hub", "cycle", "8"]) == 1
    assert "no port 8" in capsys.readouterr().err


def test_serial_number_picks_the_hub(sdk, capsys):
    sdk.units.append(FakeUnit(OTHER_SERIAL))
    assert main(["hub", "status", "--serial-number", "0badf00d"]) == 0
    assert "0x0BADF00D" in capsys.readouterr().out


def test_several_hubs_without_serial_number_fails(sdk, capsys):
    sdk.units.append(FakeUnit(OTHER_SERIAL))
    assert main(["hub", "status"]) == 1
    assert "pass serial_number" in capsys.readouterr().err


def test_bad_serial_number_is_a_usage_error(sdk, capsys):
    with pytest.raises(SystemExit) as info:
        main(["hub", "status", "--serial-number", "hub"])
    assert info.value.code == 2


def test_hub_errors_are_reported(sdk, capsys):
    sdk.units[0].failures["setPortDisable"] = fakes.brainstem.TIMEOUT
    assert main(["hub", "cycle", "1"]) == 1
    assert "Timeout occurred" in capsys.readouterr().err


def test_discover_includes_acroname_hubs(sdk, capsys):
    assert any(b.name == "acroname" for b in discover.BACKENDS)
    assert main(["discover"]) == 0
    assert "acroname: USBHub3p" in capsys.readouterr().out


def test_missing_sdk_names_the_extra(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "brainstem", None)
    assert main(["hub", "list"]) == 1
    assert "educelab-devices[acroname]" in capsys.readouterr().err
