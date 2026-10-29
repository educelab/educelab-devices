"""XLC2 tests against a real board: ``pytest -m hardware tests/test_xlc_hardware.py``.

An XLC2 can't be found automatically, so set ``XLC2_PORT`` to its port, e.g.
``/dev/cu.usbmodem00001``. The board needs firmware 2.0.0
(docs/hardware/xlc.md#flashing). Channels are lit at 25%; the 940 nm array is
invisible to the eye.
"""

import os

import pytest

from educelab.devices.errors import CommandRejected, DeviceNotFound
from educelab.devices.lights.xlc import XLC2

pytestmark = pytest.mark.hardware

LEVEL = 0.25


@pytest.fixture(scope="module")
def board():
    port = os.environ.get("XLC2_PORT")
    if not port:
        pytest.skip("set XLC2_PORT to the XLC2's serial port")
    light = XLC2(port)
    try:
        light.open()
    except DeviceNotFound as exc:
        pytest.skip(f"no XLC2: {exc}")
    print(f"\n{light.info}")
    yield light
    light.close()


@pytest.fixture
def light(board):
    board.all_off()
    yield board
    board.all_off()


def test_identifies_as_an_xlc2_on_current_firmware(light):
    assert light.info.model == "XLC2"
    assert tuple(int(p) for p in light.info.firmware_version.split(".")) >= (2, 0, 0)
    assert light.hardware_version == "1.0"


@pytest.mark.parametrize("channel", ["white", "940nm"])
def test_each_channel_is_acknowledged(light, channel):
    light.set_level(channel, LEVEL)
    assert light.level(channel) == LEVEL
    light.set_level(channel, 0.0)


# The driver never sends these, so they go to the board directly.
@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        (("B", 256), "invalid command"),
        (("B",), "invalid command"),
        (("L", 222, 9), "index out of range"),
        (("Q",), "unknown command"),
    ],
)
def test_bad_commands_are_rejected(light, fields, reason):
    with pytest.raises(CommandRejected, match=reason):
        light._command(*fields)


def test_all_off_is_acknowledged(light):
    light.set_level("white", LEVEL)
    light.set_level("940nm", LEVEL)
    light.all_off()
    assert (light.level("white"), light.level("940nm")) == (0.0, 0.0)
