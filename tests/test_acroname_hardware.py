"""USBHub3p tests against a real hub: ``pytest -m hardware tests/test_acroname_hardware.py``.

Uses the one hub attached, or ``ACRONAME_SERIAL_NUMBER`` to pick one. Tests that
switch power only touch the port named by ``ACRONAME_TEST_PORT``, and are skipped
without it: put nothing on that port you'd mind losing power.
"""

import os
import re

import pytest

from educelab.devices.errors import DeviceNotFound
from educelab.devices.hubs.acroname import AcronameHub

pytestmark = pytest.mark.hardware


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
    """The port that's safe to switch, powered before and after each test."""
    value = os.environ.get("ACRONAME_TEST_PORT")
    if value is None:
        pytest.skip("set ACRONAME_TEST_PORT to a port that's safe to switch")
    port = int(value)
    hub.power_on(port)
    yield port
    hub.power_on(port)


def test_identifies_as_a_usbhub3p(hub):
    info = hub.info
    assert info.model == "USBHub3p"
    assert re.fullmatch(r"0x[0-9A-F]{8}", info.serial_number)
    assert re.fullmatch(r"\d+\.\d+\.\d+", info.firmware_version)


def test_discover_lists_the_open_hub(hub):
    assert hub.info.serial_number in [i.serial_number for i in hub.discover()]


def test_every_port_reports_its_power(hub):
    assert all(isinstance(hub.is_powered(p), bool) for p in hub.ports)


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


def test_enumeration_delay_reads_back(hub):
    original = hub.enumeration_delay().value
    target = 0.2 if original != 0.2 else 0.3
    try:
        hub.set_enumeration_delay(target)
        assert hub.enumeration_delay().value == pytest.approx(target)
    finally:
        hub.set_enumeration_delay(original)
    assert hub.enumeration_delay().value == pytest.approx(original)
