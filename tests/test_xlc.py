import pytest

from contracts import LightContract
from educelab.devices._serial import SerialTransport
from educelab.devices.errors import (
    CommandRejected,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
)
from educelab.devices.lights import ChannelKind, xlc
from educelab.devices.lights.xlc import XLC1
from fakes.serial_port import ScriptedSerial
from fakes.xlc import XLC1_IDENTITY, XLCFirmware


@pytest.fixture(autouse=True)
def quick_handshake(monkeypatch):
    monkeypatch.setattr(xlc, "HANDSHAKE_TIMEOUT_S", 0.05)


def make_transport(fake, port="/dev/ttyXLC"):
    return SerialTransport(port, baudrate=9600, timeout=0.05, opener=fake.open)


def make_xlc1(fake, port="/dev/ttyXLC"):
    return XLC1(port, transport=make_transport(fake, port))


def opened(light):
    light.open()
    return light


def xlc1_scripted(*steps):
    """An XLC1 that identifies itself and is turned off on open, then ``steps``."""
    return ScriptedSerial([("<?>", [XLC1_IDENTITY]), ("<C,0,0,0>", ["OK"]), *steps])


def emulated(board="XLC1"):
    """A board running emulated firmware, and the firmware."""
    firmware = XLCFirmware(board)
    return ScriptedSerial(respond=firmware), firmware


def test_firmware_emulation_matches_the_documented_protocol():
    xlc1 = XLCFirmware("XLC1")
    assert xlc1("<?>") == [XLC1_IDENTITY]
    assert xlc1("<C,1,2,256>") == ["ERR invalid command"]
    assert xlc1("<C,1,2>") == ["ERR invalid command"]
    assert xlc1("<B,10>") == ["ERR unknown command"]
    assert xlc1("<C, 4 ,5, 6 >") == ["OK"]
    assert xlc1.color == (4, 5, 6)


class TestXLC1Contract(LightContract):
    @pytest.fixture
    def make_device(self):
        return lambda: make_xlc1(emulated()[0])


# XLC1: opening


def test_xlc1_open_identifies_the_board_and_turns_it_off():
    fake = xlc1_scripted()
    light = opened(make_xlc1(fake))
    fake.assert_done()
    assert light.info.backend == "xlc"
    assert light.info.model == "XLC1"
    assert light.info.address == "/dev/ttyXLC"
    assert light.info.firmware_version == "2.0.0"
    assert light.hardware_version == "1.0"
    assert light.color("rgb") == (0.0, 0.0, 0.0)


def test_xlc1_open_skips_chatter_and_ignores_name_case():
    fake = ScriptedSerial(
        [("<?>", ["", " educelab xlc1 , 1.0 , 2.1.0 "]), ("<C,0,0,0>", ["OK"])]
    )
    assert opened(make_xlc1(fake)).info.firmware_version == "2.1.0"


def test_default_transport_uses_the_xlc_port_settings():
    transport = XLC1("COM3")._transport
    assert (transport.port, transport.baudrate) == ("COM3", xlc.BAUD_RATE)
    assert transport.reset_settle == xlc.RESET_SETTLE_S


@pytest.mark.parametrize(
    "replies",
    [[], ["EduceLab XLC2,1.0,2.0.0"], ["MISHA LED Panels,1.1,2.0.0"], ["OK"]],
    ids=["silent", "XLC2", "MISHA", "stray OK"],
)
def test_xlc1_open_fails_unless_an_xlc1_answers(replies):
    fake = ScriptedSerial([("<?>", replies)])
    with pytest.raises(DeviceNotFound, match="ttyXLC"):
        opened(make_xlc1(fake))
    assert not fake.is_open


@pytest.mark.parametrize(
    "identity",
    ["EduceLab XLC1 (FW 1.1)", "EduceLab XLC1,1.0,1.9.9", "EduceLab XLC1,1.0,dev"],
    ids=["1.1 handshake", "1.9.9", "unparseable"],
)
def test_xlc1_open_refuses_old_or_unknown_firmware(identity):
    fake = ScriptedSerial([("<?>", [identity])])
    light = make_xlc1(fake)
    with pytest.raises(DeviceError, match="firmware/xlc1") as raised:
        light.open()
    assert not isinstance(raised.value, DeviceNotFound)
    assert not light.is_open
    assert not fake.is_open


def test_open_fails_when_the_port_is_missing():
    with pytest.raises(DeviceNotFound):
        opened(make_xlc1(ScriptedSerial(present=False)))


# XLC1: colour


def test_xlc1_has_one_rgb_channel():
    (channel,) = make_xlc1(ScriptedSerial()).channels
    assert (channel.id, channel.kind) == ("rgb", ChannelKind.RGB)


def test_xlc1_set_color_sends_duties_and_waits_for_ok():
    fake = xlc1_scripted(("<C,255,128,0>", ["OK"]), ("<C,0,1,254>", ["OK"]))
    light = opened(make_xlc1(fake))
    light.set_color("rgb", (1.0, 0.5, 0.0))
    light.set_color("rgb", (0.0, 0.002, 0.998))
    fake.assert_done()


def test_xlc1_set_level_sets_a_grey():
    fake, firmware = emulated()
    light = opened(make_xlc1(fake))
    light.set_level("rgb", 0.5)
    assert firmware.color == (128, 128, 128)
    assert light.color("rgb") == (0.5, 0.5, 0.5)


def test_xlc1_all_off_sends_black():
    fake, firmware = emulated()
    light = opened(make_xlc1(fake))
    light.set_color("rgb", (1.0, 1.0, 1.0))
    light.all_off()
    assert firmware.color == (0, 0, 0)
    assert light.color("rgb") == (0.0, 0.0, 0.0)


def test_err_raises_command_rejected():
    light = opened(make_xlc1(xlc1_scripted(("<C,255,0,0>", ["ERR invalid command"]))))
    with pytest.raises(CommandRejected, match="invalid command"):
        light.set_color("rgb", (1.0, 0.0, 0.0))


def test_a_rejected_color_is_not_remembered():
    light = opened(make_xlc1(xlc1_scripted(("<C,255,0,0>", ["ERR invalid command"]))))
    with pytest.raises(CommandRejected):
        light.set_color("rgb", (1.0, 0.0, 0.0))
    assert light.color("rgb") == (0.0, 0.0, 0.0)


def test_silence_raises_device_timeout():
    light = opened(make_xlc1(xlc1_scripted(("<C,255,0,0>", []))))
    with pytest.raises(DeviceTimeout):
        light.set_color("rgb", (1.0, 0.0, 0.0))


def test_a_pre_2_0_debug_line_raises_device_error():
    light = opened(make_xlc1(xlc1_scripted(("<C,255,0,0>", ["Got a color command"]))))
    with pytest.raises(DeviceError, match="Got a color command"):
        light.set_color("rgb", (1.0, 0.0, 0.0))


def test_a_late_reply_is_not_mistaken_for_the_next_one():
    fake = xlc1_scripted(("<C,255,0,0>", []), ("<C,0,255,0>", ["ERR invalid command"]))
    light = opened(make_xlc1(fake))
    with pytest.raises(DeviceTimeout):
        light.set_color("rgb", (1.0, 0.0, 0.0))
    fake.queue("OK")
    with pytest.raises(CommandRejected):
        light.set_color("rgb", (0.0, 1.0, 0.0))


# Closing


def test_close_turns_everything_off():
    fake = xlc1_scripted(("<C,0,0,0>", ["OK"]))
    light = opened(make_xlc1(fake))
    light.close()
    fake.assert_done()
    assert not fake.is_open


def test_close_after_unplugging_still_closes(caplog):
    fake = xlc1_scripted()
    light = opened(make_xlc1(fake))
    fake.unplug()
    light.close()
    assert not light.is_open
    assert "could not turn off" in caplog.text
