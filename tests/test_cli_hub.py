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


def test_discover_includes_acroname_hubs(sdk, capsys, discover_only):
    discover_only("acroname")
    assert any(b.name == "acroname" for b in discover.BACKENDS)
    assert main(["discover"]) == 0
    out = capsys.readouterr().out
    assert "acroname: 1 found" in out
    assert "USBHub3p" in out


def test_missing_sdk_names_the_extra(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "brainstem", None)
    assert main(["hub", "list"]) == 1
    assert "educelab-devices[acroname]" in capsys.readouterr().err


def test_status_shows_each_ports_limit_and_connection(sdk, capsys):
    sdk.units[0].devices[1] = "usb3"
    sdk.units[0].port_state[2] = fakes.brainstem.VBUS_ENABLED
    assert main(["hub", "status"]) == 0
    out = capsys.readouterr().out
    assert "port 1: on, limit usb3, connected usb3" in out
    assert "port 2: on, limit data-off, connected nothing" in out


def test_speed_shows_one_port(sdk, capsys):
    sdk.units[0].devices[4] = "usb3"
    assert main(["hub", "speed", "4"]) == 0
    assert "port 4: on, limit usb3, connected usb3" in capsys.readouterr().out
    assert not any(c[0].startswith("set") for c in sdk.units[0].calls)


def test_speed_sets_the_limit(sdk, capsys):
    unit = sdk.units[0]
    unit.devices[4] = "usb3"
    assert main(["hub", "speed", "4", "USB2"]) == 0
    assert "port 4: on, limit usb2, connected usb2" in capsys.readouterr().out
    assert not any(c[0].startswith(("setPort", "setPower")) for c in unit.calls)


def test_speed_with_cycle_power_cycles_and_keeps_the_limit(sdk, capsys):
    unit = sdk.units[0]
    assert main(["hub", "speed", "4", "data-off", "--cycle"]) == 0
    assert ("setPortDisable", (4,)) in unit.calls
    assert unit.port_state[4] == fakes.brainstem.VBUS_ENABLED
    assert "limit data-off" in capsys.readouterr().out


def test_unknown_speed_is_a_usage_error(sdk, capsys):
    with pytest.raises(SystemExit) as info:
        main(["hub", "speed", "4", "usb4"])
    assert info.value.code == 2
    assert "data-off" in capsys.readouterr().err


def test_speed_unknown_port_fails(sdk, capsys):
    assert main(["hub", "speed", "9", "usb2"]) == 1
    assert "no port 9" in capsys.readouterr().err


def test_cycle_without_a_speed_is_refused(sdk, capsys):
    assert main(["hub", "speed", "4", "--cycle"]) == 2
    assert not any(c[0].startswith("set") for c in sdk.units[0].calls)


def test_raising_to_usb3_warns_when_the_device_stays_at_usb2(sdk, capsys):
    sdk.units[0].devices[4] = "usb3"
    main(["hub", "speed", "4", "usb2"])
    capsys.readouterr()
    assert main(["hub", "speed", "4", "usb3"]) == 0
    captured = capsys.readouterr()
    assert "connected usb2" in captured.out
    assert "hub speed 4 usb3 --cycle" in captured.err


def test_raising_to_usb3_with_cycle_reconnects_without_warning(sdk, capsys):
    sdk.units[0].devices[4] = "usb3"
    main(["hub", "speed", "4", "usb2"])
    capsys.readouterr()
    assert main(["hub", "speed", "4", "usb3", "--cycle"]) == 0
    captured = capsys.readouterr()
    assert "connected usb3" in captured.out
    assert captured.err == ""
