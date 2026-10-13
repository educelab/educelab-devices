import pytest

from educelab.devices._serial import SerialTransport
from educelab.devices.errors import DeviceError, DeviceNotFound
from educelab.devices.lights import misha
from educelab.devices.lights.misha import MishaLight
from fakes.serial_port import ScriptedSerial

IDENTITY = "MISHA LED Panels,1.1,2.0.0"


@pytest.fixture(autouse=True)
def quick_handshake(monkeypatch):
    monkeypatch.setattr(misha, "HANDSHAKE_TIMEOUT_S", 0.05)


def make_light(fake, port="/dev/ttyMISHA"):
    transport = SerialTransport(port, baudrate=9600, timeout=0.05, opener=fake.open)
    return MishaLight(port, transport=transport)


def opened(fake):
    light = make_light(fake)
    light.open()
    return light


def test_open_identifies_the_board_and_turns_everything_off():
    fake = ScriptedSerial([("ID", [IDENTITY]), ("0,0", ["OK"])])
    light = opened(fake)
    fake.assert_done()
    assert light.info.firmware_version == "2.0.0"
    assert light.info.address == "/dev/ttyMISHA"
    assert light.hardware_version == "1.1"


def test_open_skips_chatter_and_ignores_name_case():
    fake = ScriptedSerial(
        [("ID", ["booting", " misha led panels , 1.1 , 2.1.0 "]), ("0,0", ["OK"])]
    )
    assert opened(fake).info.firmware_version == "2.1.0"


def test_default_transport_uses_the_misha_port_settings():
    transport = MishaLight("COM3")._transport
    assert (transport.port, transport.baudrate) == ("COM3", misha.BAUD_RATE)
    assert transport.reset_settle == misha.RESET_SETTLE_S


@pytest.mark.parametrize(
    "replies",
    [[], ["XLC2,1.0,1.0.0"], ["MISHA LED Panels,2.0.0"], ["470,75"]],
    ids=["silent", "other board", "two fields", "echo"],
)
def test_open_fails_unless_a_misha_answers(replies):
    fake = ScriptedSerial([("ID", replies)])
    with pytest.raises(DeviceNotFound, match="ttyMISHA"):
        opened(fake)
    assert not fake.is_open


@pytest.mark.parametrize("version", ["1.0.0", "1.9", "dev"])
def test_open_refuses_old_or_unknown_firmware(version):
    fake = ScriptedSerial([("ID", [f"MISHA LED Panels,1.1,{version}"])])
    light = make_light(fake)
    with pytest.raises(DeviceError, match="firmware/misha") as raised:
        light.open()
    assert not isinstance(raised.value, DeviceNotFound)
    assert not light.is_open
    assert not fake.is_open


def test_open_fails_when_the_port_is_missing():
    with pytest.raises(DeviceNotFound):
        opened(ScriptedSerial(present=False))
