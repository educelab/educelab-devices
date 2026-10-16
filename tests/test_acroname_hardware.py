"""USBHub3p tests against a real hub: ``pytest -m hardware tests/test_acroname_hardware.py``.

Uses the one hub attached, or ``ACRONAME_SERIAL_NUMBER`` to pick one. Tests that
switch power or speed only touch the port named by ``ACRONAME_TEST_PORT``, and
are skipped without it. That port must have nothing important on it: on a rig,
the other ports usually carry cameras in use. The test port's power and speed
are restored after each test. The enumeration delay test changes the hub-wide
delay briefly and restores it.

Set ``ACRONAME_USB3_DEVICE=1`` when a spare USB 3 device is plugged into the test
port, to check the speed it connects at under each limit. Run with ``-s`` to see
how long the device took to reconnect each time.
"""

import os
import re
import time

import pytest

from educelab.devices.errors import DeviceNotFound
from educelab.devices.hubs.acroname import AcronameHub, PortSpeed

pytestmark = pytest.mark.hardware

# Loose: re-enumeration time depends on the device and host (see the hardware doc).
RECONNECT_TIMEOUT_S = 15.0
# After a power cycle the device also has to boot.
POWER_CYCLE_RECONNECT_TIMEOUT_S = 30.0


def make_hub() -> AcronameHub:
    return AcronameHub(os.environ.get("ACRONAME_SERIAL_NUMBER"))


@pytest.fixture(scope="module")
def hub():
    device = make_hub()
    try:
        device.open()
    except DeviceNotFound as exc:
        pytest.skip(f"no USBHub3p: {exc}")
    yield device
    device.close()


@pytest.fixture
def port(hub):
    """The port that's safe to switch: powered at USB3 for the test, then restored."""
    value = os.environ.get("ACRONAME_TEST_PORT")
    if value is None:
        pytest.skip("set ACRONAME_TEST_PORT to a port with nothing important on it")
    port = int(value)
    assert port in hub.ports, f"ACRONAME_TEST_PORT={port} isn't one of {hub.ports}"
    was_powered = hub.is_powered(port)
    was_speed = hub.port_speed(port)
    try:
        hub.set_port_speed(port, PortSpeed.USB3)
        hub.power_on(port)
        yield port
    finally:
        hub.open()  # a test may have closed it
        hub.set_port_speed(port, was_speed)
        if was_powered:
            hub.power_on(port)
        else:
            hub.power_off(port)


@pytest.fixture
def usb3_device(hub, port):
    """The test port, with a USB 3 device connected at USB 3 on it."""
    if os.environ.get("ACRONAME_USB3_DEVICE") != "1":
        pytest.skip("set ACRONAME_USB3_DEVICE=1 with a USB 3 device on the test port")
    # The port fixture may just have powered the port or restored its data lines.
    speed, waited = wait_for_connected_speed(hub, port, PortSpeed.USB3)
    assert speed is PortSpeed.USB3, (
        f"ACRONAME_USB3_DEVICE=1, but no USB 3 device connected on port {port} "
        f"(ACRONAME_TEST_PORT) within {waited:.1f} s. Plug a spare USB 3 device "
        f"into port {port}, or unset ACRONAME_USB3_DEVICE. {describe(hub, port)}"
    )
    return port


def reopened_speed(hub, port):
    """The port's speed as a fresh connection reads it from the hub."""
    hub.close()
    try:
        with make_hub() as fresh:
            return fresh.port_speed(port)
    finally:
        hub.open()


def wait_for_connected_speed(hub, port, expected, timeout=RECONNECT_TIMEOUT_S):
    """Poll until the port's connected speed is ``expected``.

    Returns the last speed read and the seconds waited.
    """
    start = time.monotonic()
    while (speed := hub.connected_speed(port)) != expected:
        if time.monotonic() - start > timeout:
            break
        time.sleep(0.25)
    waited = time.monotonic() - start
    print(f"port {port}: {speed} after {waited:.1f} s (wanted {expected})")
    return speed, waited


def describe(hub, port):
    """The port's raw state, for failure messages: read-only SDK getters."""
    state = hub._usb.getPortState(port)
    current = hub._usb.getPortCurrent(port)
    names = {0: "VBUS", 1: "USB2 data", 3: "USB3 data", 11: "at USB2", 12: "at USB3"}
    names[23] = "attached"
    bits = [names.get(b, str(b)) for b in range(32) if state.value >> b & 1]
    return (
        f"Port state 0x{state.value:08X} {bits} (error {state.error}), "
        f"current {current.value} uA (error {current.error})."
    )


def test_identifies_as_a_usbhub3p(hub):
    info = hub.info
    assert info.model == "USBHub3p"
    assert re.fullmatch(r"0x[0-9A-F]{8}", info.serial_number)
    assert re.fullmatch(r"\d+\.\d+\.\d+", info.firmware_version)


def test_discover_lists_the_open_hub(hub):
    assert hub.info.serial_number in [i.serial_number for i in hub.discover()]


def test_every_port_reports_its_power_and_speed(hub):
    for p in hub.ports:
        assert isinstance(hub.is_powered(p), bool)
        assert isinstance(hub.port_speed(p), PortSpeed)
        assert hub.connected_speed(p) in (None, PortSpeed.USB2, PortSpeed.USB3)


def test_power_off_and_on(hub, port):
    hub.power_off(port)
    assert not hub.is_powered(port)
    hub.power_on(port)
    assert hub.is_powered(port)


def test_power_cycle_leaves_the_port_powered(hub, port):
    hub.power_cycle(port)
    assert hub.is_powered(port)


def test_a_port_switched_off_stays_off_across_a_reconnect(hub, port):
    hub.power_off(port)
    hub.close()
    try:
        with make_hub() as reopened:
            assert not reopened.is_powered(port)
    finally:
        hub.open()


@pytest.mark.parametrize("speed", [PortSpeed.USB2, PortSpeed.DATA_OFF, PortSpeed.USB3])
def test_speed_limit_reaches_the_hub(hub, port, speed):
    hub.set_port_speed(port, speed)
    assert hub.is_powered(port)
    assert reopened_speed(hub, port) is speed


@pytest.mark.parametrize("speed", [PortSpeed.USB2, PortSpeed.DATA_OFF])
def test_speed_limit_survives_power_off_and_on(hub, port, speed):
    hub.set_port_speed(port, speed)
    hub.power_off(port)
    hub.power_on(port)
    assert reopened_speed(hub, port) is speed
    hub.power_cycle(port)
    assert reopened_speed(hub, port) is speed


def test_usb2_limit_applies_without_a_power_cycle(hub, usb3_device):
    port = usb3_device
    hub.set_port_speed(port, PortSpeed.USB2)
    speed, waited = wait_for_connected_speed(hub, port, PortSpeed.USB2)
    assert speed is PortSpeed.USB2, f"after {waited:.1f} s. {describe(hub, port)}"


def test_data_off_disconnects_the_device(hub, usb3_device):
    port = usb3_device
    hub.set_port_speed(port, PortSpeed.DATA_OFF)
    speed, waited = wait_for_connected_speed(hub, port, None)
    assert speed is None, f"after {waited:.1f} s. {describe(hub, port)}"


def test_restoring_usb3_without_a_power_cycle(hub, usb3_device):
    # Whether the hub or device renegotiates up without a reset is unknown; a
    # failure here is a finding for the hardware doc, not necessarily a bug.
    port = usb3_device
    hub.set_port_speed(port, PortSpeed.USB2)
    wait_for_connected_speed(hub, port, PortSpeed.USB2)
    hub.set_port_speed(port, PortSpeed.USB3)
    speed, waited = wait_for_connected_speed(hub, port, PortSpeed.USB3)
    assert speed is PortSpeed.USB3, f"after {waited:.1f} s. {describe(hub, port)}"


@pytest.mark.parametrize("speed", [PortSpeed.USB3, PortSpeed.USB2])
def test_device_reconnects_after_a_power_cycle(hub, usb3_device, speed):
    port = usb3_device
    hub.set_port_speed(port, speed)
    wait_for_connected_speed(hub, port, speed)
    hub.power_cycle(port)
    connected, waited = wait_for_connected_speed(
        hub, port, speed, timeout=POWER_CYCLE_RECONNECT_TIMEOUT_S
    )
    assert connected is speed, (
        f"not reconnected at {speed} {waited:.1f} s after the power cycle. "
        f"{describe(hub, port)}"
    )


def test_enumeration_delay_reads_back(hub):
    original = hub.enumeration_delay().value
    target = 0.2 if original != 0.2 else 0.3
    try:
        hub.set_enumeration_delay(target)
        assert hub.enumeration_delay().value == pytest.approx(target)
    finally:
        hub.set_enumeration_delay(original)
    assert hub.enumeration_delay().value == pytest.approx(original)
