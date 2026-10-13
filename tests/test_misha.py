import pytest

from educelab.devices._serial import SerialTransport
from educelab.devices.errors import (
    CommandRejected,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
)
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


def scripted(*steps):
    return ScriptedSerial([("ID", [IDENTITY]), ("0,0", ["OK"]), *steps])


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


def test_channels_are_the_sixteen_bands():
    light = make_light(ScriptedSerial())
    assert [c.id for c in light.channels][:3] == ["365nm", "385nm", "395nm"]
    assert len(light.channels) == 16
    assert light.channels[-1].wavelength_nm == 940.0


def test_set_level_sends_percent_and_waits_for_ok():
    fake = scripted(("450,50", ["OK"]), ("940,26", ["OK"]))
    light = opened(fake)
    light.set_level("450nm", 0.5)
    light.set_level("940nm", 0.256)
    fake.assert_done()
    assert light.level("940nm") == 0.26


def test_lighting_a_band_turns_the_others_off():
    light = opened(scripted(("450,50", ["OK"]), ("365,100", ["OK"])))
    light.set_level("450nm", 0.5)
    light.set_level("365nm", 1.0)
    assert light.level("365nm") == 1.0
    assert light.level("450nm") == 0.0


def test_everything_is_off_after_open():
    light = opened(scripted())
    assert all(light.level(c.id) == 0.0 for c in light.channels)


def test_turning_off_the_lit_band_turns_everything_off():
    fake = scripted(("450,50", ["OK"]), ("0,0", ["OK"]))
    light = opened(fake)
    light.set_level("450nm", 0.5)
    light.set_level("450nm", 0.0)
    fake.assert_done()
    assert light.level("450nm") == 0.0


def test_turning_off_an_unlit_band_leaves_the_lit_one_alone():
    fake = scripted(("450,50", ["OK"]))
    light = opened(fake)
    light.set_level("450nm", 0.5)
    light.set_level("365nm", 0.0)
    assert fake.written[-1] == "450,50"
    assert light.level("450nm") == 0.5


def test_all_off_sends_one_command():
    fake = scripted(("450,50", ["OK"]), ("0,0", ["OK"]))
    light = opened(fake)
    light.set_level("450nm", 0.5)
    light.all_off()
    fake.assert_done()
    assert light.level("450nm") == 0.0


def test_err_raises_command_rejected_and_keeps_state():
    light = opened(
        scripted(("450,50", ["OK"]), ("365,100", ["ERR unknown wavelength"]))
    )
    light.set_level("450nm", 0.5)
    with pytest.raises(CommandRejected, match="unknown wavelength"):
        light.set_level("365nm", 1.0)
    assert light.level("450nm") == 0.5


def test_silence_raises_device_timeout():
    light = opened(scripted(("450,50", [])))
    with pytest.raises(DeviceTimeout):
        light.set_level("450nm", 0.5)


def test_unexpected_reply_raises_device_error():
    light = opened(scripted(("450,50", ["Invalid command"])))
    with pytest.raises(DeviceError, match="Invalid command"):
        light.set_level("450nm", 0.5)


def test_a_late_reply_is_not_mistaken_for_the_next_one():
    fake = scripted(("450,50", []), ("365,100", ["ERR unknown wavelength"]))
    light = opened(fake)
    with pytest.raises(DeviceTimeout):
        light.set_level("450nm", 0.5)
    fake.queue("OK")
    with pytest.raises(CommandRejected):
        light.set_level("365nm", 1.0)


def test_unknown_band_is_rejected_before_sending():
    fake = scripted()
    light = opened(fake)
    with pytest.raises(KeyError):
        light.set_level("600nm", 0.5)
    assert fake.written == ["ID", "0,0"]


def test_close_turns_everything_off():
    fake = scripted(("450,50", ["OK"]), ("0,0", ["OK"]))
    light = opened(fake)
    light.set_level("450nm", 0.5)
    light.close()
    fake.assert_done()
    assert not fake.is_open


def test_close_after_unplugging_still_closes(caplog):
    fake = scripted()
    light = opened(fake)
    fake.unplug()
    light.close()
    assert not light.is_open
    assert "could not turn off" in caplog.text
