import importlib
import math
import subprocess
import sys
import time

import pytest

import fakes.brainstem
from contracts import HubContract
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import (
    CommandRejected,
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    NotConnected,
)
from educelab.devices.hubs import acroname
from educelab.devices.hubs.acroname import AcronameHub, PortSpeed
from fakes.brainstem import FakeBrainstem, FakeUnit


def test_importing_the_driver_does_not_import_brainstem():
    code = (
        "import sys; import educelab.devices.hubs.acroname; "
        "assert 'brainstem' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_missing_brainstem_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "brainstem", None)
    with pytest.raises(ImportError, match=r"educelab-devices\[acroname\]"):
        acroname.load_sdk()


def test_brainstem_library_load_failure_is_an_import_error(monkeypatch):
    # On Windows without the MSVC runtime, brainstem raises OSError at import.
    def fail(name):
        raise OSError("missing MSVC redistributable")

    monkeypatch.setattr(importlib, "import_module", fail)
    with pytest.raises(ImportError, match="acroname-usbhub3p.md") as info:
        acroname.load_sdk()
    assert isinstance(info.value.__cause__, OSError)


# Driver behaviour, on a fake BrainStem SDK.

SERIAL = 0x1234ABCD
OTHER_SERIAL = 0x0BADF00D


def bus(*units):
    return FakeBrainstem(*(units or (FakeUnit(SERIAL),)))


@pytest.fixture
def sdk(monkeypatch):
    sdk = bus()
    monkeypatch.setattr(acroname, "load_sdk", lambda: sdk)
    return sdk


@pytest.fixture
def unit(sdk):
    return sdk.unit(SERIAL)


@pytest.fixture
def hub(sdk):
    with AcronameHub(SERIAL, sdk=sdk) as hub:
        yield hub


class TestAcronameHubContract(HubContract):
    @pytest.fixture(autouse=True)
    def fake_sdk(self, monkeypatch):
        monkeypatch.setattr(acroname, "load_sdk", bus)

    @pytest.fixture
    def make_device(self):
        return lambda: AcronameHub(SERIAL, sdk=bus())


def test_discover_lists_hubs_by_serial_number(monkeypatch):
    sdk = bus(
        FakeUnit(SERIAL),
        FakeUnit(0x1, model=fakes.brainstem.OTHER_MODEL),
        FakeUnit(OTHER_SERIAL),
    )
    monkeypatch.setattr(acroname, "load_sdk", lambda: sdk)
    assert AcronameHub.discover() == [
        DeviceInfo(backend="acroname", model="USBHub3p", serial_number="0x1234ABCD"),
        DeviceInfo(backend="acroname", model="USBHub3p", serial_number="0x0BADF00D"),
    ]
    assert all(u.connections == 0 for u in sdk.units)


def test_constructor_does_not_load_the_sdk(monkeypatch):
    def fail():
        raise AssertionError("loaded the SDK")

    monkeypatch.setattr(acroname, "load_sdk", fail)
    AcronameHub(SERIAL)


@pytest.mark.parametrize("serial", [SERIAL, "0x1234abcd", "1234ABCD"])
def test_serial_number_accepts_int_or_hex_string(serial, sdk, unit):
    with AcronameHub(serial, sdk=sdk) as hub:
        assert unit.connections == 1
        assert hub.info.serial_number == "0x1234ABCD"


@pytest.mark.parametrize("serial", ["", "hub", 0, "0x0", -1, 1 << 32, 1.0, True])
def test_bad_serial_number_raises(serial):
    with pytest.raises((ValueError, TypeError)):
        AcronameHub(serial)


def test_open_reports_identity(hub):
    assert hub.info == DeviceInfo(
        backend="acroname",
        model="USBHub3p",
        serial_number="0x1234ABCD",
        firmware_version="2.12.0",
    )
    assert "0x1234ABCD" in repr(hub)


def test_open_without_serial_number_uses_the_only_hub(sdk, unit):
    with AcronameHub(sdk=sdk) as hub:
        assert unit.connections == 1
        assert hub.info.serial_number == "0x1234ABCD"


def test_open_without_serial_number_refuses_to_guess_between_hubs():
    sdk = bus(FakeUnit(SERIAL), FakeUnit(OTHER_SERIAL))
    with pytest.raises(DeviceNotFound, match="0x0BADF00D"):
        AcronameHub(sdk=sdk).open()
    assert all(u.connections == 0 for u in sdk.units)


def test_open_without_any_hub_raises_device_not_found():
    with pytest.raises(DeviceNotFound):
        AcronameHub(sdk=FakeBrainstem()).open()


def test_open_with_unknown_serial_number_raises_device_not_found(sdk):
    hub = AcronameHub(OTHER_SERIAL, sdk=sdk)
    with pytest.raises(DeviceNotFound, match="0x0BADF00D"):
        hub.open()
    assert not hub.is_open


def test_open_on_a_busy_hub_raises_device_busy(sdk, unit):
    unit.busy = True
    with pytest.raises(DeviceBusy):
        AcronameHub(SERIAL, sdk=sdk).open()


def test_open_disconnects_if_identification_fails(sdk, unit):
    unit.failures["getVersion"] = fakes.brainstem.TIMEOUT
    hub = AcronameHub(SERIAL, sdk=sdk)
    with pytest.raises(DeviceTimeout):
        hub.open()
    assert unit.connections == 0
    assert not hub.is_open


def test_close_disconnects(sdk, unit):
    hub = AcronameHub(SERIAL, sdk=sdk)
    hub.open()
    hub.close()
    assert unit.connections == 0


def test_close_after_unplug_still_closes(sdk, unit):
    hub = AcronameHub(SERIAL, sdk=sdk)
    hub.open()
    unit.failures["disconnect"] = fakes.brainstem.CONNECTION_ERROR
    hub.close()
    assert not hub.is_open


def test_ports_are_the_eight_downstream_ports(hub):
    assert hub.ports == tuple(range(8))


def test_power_off_and_on_disable_and_enable_the_port(hub, unit):
    hub.power_off(3)
    assert ("setPortDisable", (3,)) in unit.calls
    assert not unit.port_state[3] & fakes.brainstem.VBUS_ENABLED
    hub.power_on(3)
    assert ("setPortEnable", (3,)) in unit.calls
    assert unit.port_state[3] & fakes.brainstem.VBUS_ENABLED


def test_is_powered_reads_vbus_from_the_port_state(hub, unit):
    unit.port_state[2] = fakes.brainstem.USB2_DATA_ENABLED
    assert not hub.is_powered(2)
    unit.port_state[2] = fakes.brainstem.VBUS_ENABLED
    assert hub.is_powered(2)


def test_power_cycle_disables_waits_and_enables(hub, unit, monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    hub.power_cycle(5, off_time=2.5)
    assert [c for c in unit.calls if c[1] == (5,) and c[0].startswith("set")] == [
        ("setPortDisable", (5,)),
        ("setPortEnable", (5,)),
    ]
    assert sleeps == [2.5]
    assert hub.is_powered(5)


def test_enumeration_delay_is_a_setting_in_seconds(hub, unit):
    unit.enumeration_delay_ms = 300
    setting = hub.enumeration_delay()
    assert setting.name == "enumeration_delay"
    assert setting.value == pytest.approx(0.3)
    assert setting.unit == "s"
    assert setting.minimum == 0
    assert setting.step == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("seconds", "ms"), [(0.5, 500), (2, 2000), (0.26, 300), (0.24, 200), (0, 0)]
)
def test_set_enumeration_delay_writes_whole_steps_of_100_ms(hub, unit, seconds, ms):
    hub.set_enumeration_delay(seconds)
    assert unit.enumeration_delay_ms == ms


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (-0.1, ValueError),
        ("1", TypeError),
        (math.nan, ValueError),
        (math.inf, ValueError),
        (5e6, ValueError),
    ],
)
def test_set_enumeration_delay_validates(hub, unit, value, error):
    with pytest.raises(error):
        hub.set_enumeration_delay(value)
    assert not any(name == "setEnumerationDelay" for name, _ in unit.calls)


def test_enumeration_delay_needs_an_open_hub(sdk):
    with pytest.raises(NotConnected):
        AcronameHub(SERIAL, sdk=sdk).enumeration_delay()


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (fakes.brainstem.TIMEOUT, DeviceTimeout),
        (fakes.brainstem.BUSY, DeviceBusy),
        (fakes.brainstem.CONNECTION_ERROR, NotConnected),
        (fakes.brainstem.PARAMETER_ERROR, CommandRejected),
        (fakes.brainstem.RANGE_ERROR, CommandRejected),
        (fakes.brainstem.PERMISSION_ERROR, DeviceError),
        (99, DeviceError),
    ],
)
def test_brainstem_errors_map_to_device_errors(hub, unit, code, error):
    unit.failures["setPortDisable"] = code
    with pytest.raises(error) as info:
        hub.power_off(1)
    message = str(info.value)
    assert "0x1234ABCD" in message
    assert f"BrainStem error {code}" in message


def test_error_message_explains_the_code(hub, unit):
    unit.failures["getPortState"] = fakes.brainstem.TIMEOUT
    with pytest.raises(DeviceTimeout, match="Timeout occurred"):
        hub.is_powered(0)


def test_permission_error_points_to_udev_rules(hub, unit):
    unit.failures["setPortEnable"] = fakes.brainstem.PERMISSION_ERROR
    with pytest.raises(DeviceError, match="udev"):
        hub.power_on(0)


def test_unplugged_hub_raises_not_connected(hub, unit):
    unit.present = False
    with pytest.raises(NotConnected):
        hub.is_powered(0)


def test_failed_disconnect_after_failed_open_reports_the_open_failure(sdk, unit):
    unit.failures["getVersion"] = fakes.brainstem.TIMEOUT
    unit.failures["disconnect"] = fakes.brainstem.IO_ERROR
    with pytest.raises(DeviceTimeout):
        AcronameHub(SERIAL, sdk=sdk).open()


def test_hub_found_without_serial_number_is_the_one_reopened(sdk, unit):
    hub = AcronameHub(sdk=sdk)
    with hub:
        pass
    sdk.units.insert(0, FakeUnit(OTHER_SERIAL))
    with hub:
        assert hub.info.serial_number == "0x1234ABCD"


# Port speed.

USB2 = fakes.brainstem.USB2_DATA_ENABLED
USB3 = fakes.brainstem.USB3_DATA_ENABLED
VBUS = fakes.brainstem.VBUS_ENABLED


def lines(unit, port):
    return unit.port_state[port] & (VBUS | USB2 | USB3)


def test_ports_default_to_usb3(hub):
    assert all(hub.port_speed(p) is PortSpeed.USB3 for p in hub.ports)


@pytest.mark.parametrize(
    ("speed", "expected"),
    [
        (PortSpeed.USB2, VBUS | USB2),
        (PortSpeed.DATA_OFF, VBUS),
        (PortSpeed.USB3, VBUS | USB2 | USB3),
        ("usb2", VBUS | USB2),
    ],
)
def test_set_port_speed_switches_data_lines_only(hub, unit, speed, expected):
    hub.set_port_speed(2, speed)
    assert lines(unit, 2) == expected
    assert hub.port_speed(2) == PortSpeed(speed)
    assert not any(name.startswith(("setPort", "setPower")) for name, _ in unit.calls)


def test_restoring_usb3_reenables_both_data_lines(hub, unit):
    hub.set_port_speed(2, PortSpeed.DATA_OFF)
    hub.set_port_speed(2, PortSpeed.USB3)
    assert lines(unit, 2) == VBUS | USB2 | USB3


@pytest.mark.parametrize(
    ("speed", "expected"),
    [
        (PortSpeed.USB2, VBUS | USB2),
        (PortSpeed.DATA_OFF, VBUS),
        (PortSpeed.USB3, VBUS | USB2 | USB3),
    ],
)
def test_power_on_keeps_the_port_speed(hub, unit, speed, expected, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    hub.set_port_speed(4, speed)
    hub.power_off(4)
    assert lines(unit, 4) == 0
    hub.power_on(4)
    assert lines(unit, 4) == expected
    hub.power_cycle(4)
    assert lines(unit, 4) == expected


def test_speed_set_on_an_unpowered_port_applies_at_power_on(hub, unit):
    hub.power_off(1)
    hub.set_port_speed(1, PortSpeed.USB2)
    assert lines(unit, 1) == 0
    assert hub.port_speed(1) is PortSpeed.USB2
    hub.power_on(1)
    assert lines(unit, 1) == VBUS | USB2


@pytest.mark.parametrize(
    ("state", "speed"),
    [
        (VBUS | USB2 | USB3, PortSpeed.USB3),
        (VBUS | USB2, PortSpeed.USB2),
        (VBUS, PortSpeed.DATA_OFF),
        (0, PortSpeed.USB3),
    ],
)
def test_open_reads_each_ports_speed_from_the_hub(sdk, unit, state, speed):
    unit.port_state[5] = state
    with AcronameHub(SERIAL, sdk=sdk) as hub:
        assert hub.port_speed(5) is speed
        hub.power_on(5)
        assert hub.port_speed(5) is speed


def test_reopening_forgets_speeds_set_while_off(sdk, unit):
    with AcronameHub(SERIAL, sdk=sdk) as hub:
        hub.power_off(3)
        hub.set_port_speed(3, PortSpeed.USB2)
    with AcronameHub(SERIAL, sdk=sdk) as hub:
        assert hub.port_speed(3) is PortSpeed.USB3


@pytest.mark.parametrize(
    ("device", "speed", "connected"),
    [
        ("usb3", PortSpeed.USB3, PortSpeed.USB3),
        ("usb3", PortSpeed.USB2, PortSpeed.USB2),
        ("usb3", PortSpeed.DATA_OFF, None),
        ("usb2", PortSpeed.USB3, PortSpeed.USB2),
    ],
)
def test_connected_speed_reads_the_negotiated_speed(
    hub, unit, device, speed, connected
):
    unit.devices[6] = device
    hub.set_port_speed(6, speed)
    assert hub.connected_speed(6) == connected


def test_connected_speed_with_nothing_attached_is_none(hub):
    assert hub.connected_speed(0) is None


def test_unknown_speed_raises_value_error(hub, unit):
    with pytest.raises(ValueError):
        hub.set_port_speed(0, "usb4")
    assert lines(unit, 0) == VBUS | USB2 | USB3


@pytest.mark.parametrize("method", ["port_speed", "connected_speed"])
def test_speed_methods_reject_unknown_ports(hub, method):
    with pytest.raises(KeyError):
        getattr(hub, method)(8)


def test_set_port_speed_rejects_unknown_ports(hub):
    with pytest.raises(KeyError):
        hub.set_port_speed(8, PortSpeed.USB2)


def test_speed_methods_need_an_open_hub(sdk):
    with pytest.raises(NotConnected):
        AcronameHub(SERIAL, sdk=sdk).port_speed(0)


def test_failed_speed_change_is_a_device_error(hub, unit):
    unit.failures["setSuperSpeedDataDisable"] = fakes.brainstem.TIMEOUT
    with pytest.raises(DeviceTimeout, match="port 2"):
        hub.set_port_speed(2, PortSpeed.USB2)
    assert hub.port_speed(2) is PortSpeed.USB3
