"""MISHA tests against a real panel: ``pytest -m hardware tests/test_misha_hardware.py``.

Uses the one named panel attached, or ``MISHA_SERIAL_NUMBER`` to pick one. Bands
are lit at 25% to spare the eyes of anyone watching.
"""

import os

import pytest

from educelab.devices.errors import CommandRejected, DeviceNotFound
from educelab.devices.lights.misha import CHANNELS, MishaLight

pytestmark = pytest.mark.hardware

LEVEL = 0.25


def make_light() -> MishaLight:
    return MishaLight(serial_number=os.environ.get("MISHA_SERIAL_NUMBER"))


@pytest.fixture(scope="module")
def panel():
    light = make_light()
    try:
        light.open()
    except DeviceNotFound as exc:
        pytest.skip(f"no MISHA panel: {exc}")
    yield light
    light.close()


@pytest.fixture
def light(panel):
    panel.all_off()
    yield panel
    panel.all_off()


def lit(light):
    return {c.id: level for c in light.channels if (level := light.level(c.id))}


def test_identifies_as_a_misha_on_current_firmware(light):
    info = light.info
    assert info.model == "MISHA"
    assert tuple(int(p) for p in info.firmware_version.split(".")) >= (2, 0, 0)
    assert light.hardware_version


@pytest.mark.parametrize("channel", [c.id for c in CHANNELS])
def test_each_band_lights_alone_and_turns_off(light, channel):
    light.set_level(channel, LEVEL)
    assert lit(light) == {channel: LEVEL}
    light.set_level(channel, 0.0)
    assert lit(light) == {}


def test_lighting_a_band_turns_off_the_lit_one(light):
    light.set_level("450nm", LEVEL)
    light.set_level("530nm", LEVEL)
    assert lit(light) == {"530nm": LEVEL}


def test_changing_the_lit_bands_level_keeps_it_lit(light):
    light.set_level("530nm", LEVEL)
    light.set_level("530nm", 0.5)
    assert lit(light) == {"530nm": 0.5}


def test_zeroing_an_unlit_band_leaves_the_lit_one(light):
    light.set_level("530nm", LEVEL)
    light.set_level("450nm", 0.0)
    assert lit(light) == {"530nm": LEVEL}


def test_all_off(light):
    light.set_level("530nm", LEVEL)
    light.all_off()
    assert lit(light) == {}


# The driver refuses these before sending, so they go to the panel directly.
@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("600,25", "unknown wavelength"),
        ("530,101", "intensity out of range"),
        ("530,25x", "invalid command"),
    ],
)
def test_rejected_commands_change_nothing(light, command, reason):
    light.set_level("450nm", LEVEL)
    with pytest.raises(CommandRejected, match=reason):
        light._command(command)
    assert lit(light) == {"450nm": LEVEL}


def test_reopening_resets_the_panel_dark(panel):
    panel.set_level("530nm", LEVEL)
    # Drop the port without the OFF that close() sends.
    panel._require_transport().close()
    try:
        with make_light() as reopened:
            assert lit(reopened) == {}
    finally:
        panel._require_transport().open()
        panel.all_off()
