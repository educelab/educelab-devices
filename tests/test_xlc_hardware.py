"""XLC2 tests against a real board: ``pytest -m hardware tests/test_xlc_hardware.py``.

Set ``XLC2_PORT`` to the board's port, e.g. ``/dev/cu.usbmodem00001``, or leave
it unset to probe for the only XLC2 attached. Probing resets every board with an
XLC2's USB bridge. Set ``XLC2_UNIT_ID`` to also store that unit ID on the board
and find it again by it; the ID stays on the board. The board needs firmware
2.0.0, or 2.1.0 for the unit ID tests (docs/hardware/xlc2.md#flashing).
Channels are lit at 25%; the 940 nm array is invisible to the eye.
"""

import os

import pytest

from educelab.devices._serial import parse_version
from educelab.devices.errors import CommandRejected, DeviceNotFound
from educelab.devices.lights.xlc import XLC2

pytestmark = pytest.mark.hardware

LEVEL = 0.25


@pytest.fixture(scope="module")
def board():
    port = os.environ.get("XLC2_PORT")
    if not port:
        found = XLC2.probe()
        print(f"\nprobed: {found}")
        if len(found) != 1:
            pytest.skip(f"found {len(found)} XLC2s; set XLC2_PORT to choose one")
        port = found[0].address
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


def test_probe_finds_the_board(board):
    board.close()
    try:
        found = {i.address: i for i in XLC2.probe()}
    finally:
        board.open()
    print(f"probed: {list(found.values())}")
    assert found[board.info.address].firmware_version == board.info.firmware_version
    assert found[board.info.address].serial_number == board.info.serial_number


@pytest.fixture
def unit_id_firmware(light):
    if parse_version(light.info.firmware_version or "") < (2, 1, 0):
        pytest.skip("the unit ID needs firmware 2.1.0")
    return light


def test_an_invalid_unit_id_is_rejected(unit_id_firmware):
    with pytest.raises(CommandRejected, match="invalid command"):
        unit_id_firmware._command("U", "a b")


def test_unit_id_is_stored_and_found_again(board, unit_id_firmware):
    unit_id = os.environ.get("XLC2_UNIT_ID")
    if not unit_id:
        pytest.skip("set XLC2_UNIT_ID to store a unit ID on the board")
    board.set_unit_id(unit_id)
    board.close()
    try:
        with XLC2(serial_number=unit_id) as found:
            assert found.info.serial_number == unit_id
            assert found.info.address == board.info.address
    finally:
        board.open()
    assert board.info.serial_number == unit_id
