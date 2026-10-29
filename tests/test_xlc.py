import pytest

from contracts import LightContract
from educelab.devices import _serial
from educelab.devices._serial import PortInfo, SerialTransport
from educelab.devices.errors import (
    CommandRejected,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
)
from educelab.devices.lights import ChannelKind, xlc
from educelab.devices.lights.xlc import XLC2
from fakes.serial_port import ScriptedSerial
from fakes.xlc import XLC2_IDENTITY, XLCFirmware


@pytest.fixture(autouse=True)
def quick_handshake(monkeypatch):
    monkeypatch.setattr(xlc, "HANDSHAKE_TIMEOUT_S", 0.05)


def make_transport(fake, port="/dev/ttyXLC"):
    return SerialTransport(port, baudrate=9600, timeout=0.05, opener=fake.open)


def make_xlc2(fake, port="/dev/ttyXLC"):
    return XLC2(port, transport=make_transport(fake, port))


def opened(light):
    light.open()
    return light


def scripted(*steps):
    """An XLC2 that identifies itself and is turned off on open, then ``steps``."""
    return ScriptedSerial(
        [("<?>", [XLC2_IDENTITY]), ("<B,0>", ["OK"]), ("<X,0>", ["OK"]), *steps]
    )


def emulated():
    """A board running emulated firmware, and the firmware."""
    firmware = XLCFirmware()
    return ScriptedSerial(respond=firmware), firmware


def test_firmware_emulation_matches_the_documented_protocol():
    firmware = XLCFirmware()
    assert firmware("<?>") == [XLC2_IDENTITY]
    assert firmware("<B,256>") == ["ERR invalid command"]
    assert firmware("<B>") == ["ERR invalid command"]
    assert firmware("<C,1,2,3>") == ["ERR unknown command"]
    assert firmware("<L,222,9>") == ["ERR index out of range"]
    assert firmware("<S,1,2,3,4>") == ["OK"]
    assert firmware.pixels[54:56] == [1, 2]
    assert firmware("<X, 7 >") == ["OK"]
    assert firmware.xl4 == 7


class TestXLC2Contract(LightContract):
    @pytest.fixture
    def make_device(self):
        return lambda: make_xlc2(emulated()[0])


# Opening


def test_open_identifies_the_board_and_turns_both_outputs_off():
    fake = scripted()
    light = opened(make_xlc2(fake))
    fake.assert_done()
    assert light.info.backend == "xlc"
    assert light.info.model == "XLC2"
    assert light.info.address == "/dev/ttyXLC"
    assert light.info.firmware_version == "2.0.0"
    assert light.hardware_version == "1.0"
    assert light.level("white") == 0.0
    assert light.level("940nm") == 0.0


def test_open_skips_chatter_and_ignores_name_case():
    fake = ScriptedSerial(
        [
            ("<?>", ["", " educelab xlc2 , 1.0 , 2.1.0 "]),
            ("<B,0>", ["OK"]),
            ("<X,0>", ["OK"]),
        ]
    )
    assert opened(make_xlc2(fake)).info.firmware_version == "2.1.0"


def test_default_transport_uses_the_xlc_port_settings():
    transport = XLC2("COM3")._transport
    assert (transport.port, transport.baudrate) == ("COM3", xlc.BAUD_RATE)
    assert transport.reset_settle == xlc.RESET_SETTLE_S


@pytest.mark.parametrize(
    "replies",
    [[], ["EduceLab XLC1,1.0,2.0.0"], ["MISHA LED Panels,1.1,2.0.0"], ["OK"]],
    ids=["silent", "XLC1", "MISHA", "stray OK"],
)
def test_open_fails_unless_an_xlc2_answers(replies):
    fake = ScriptedSerial([("<?>", replies)])
    with pytest.raises(DeviceNotFound, match="ttyXLC"):
        opened(make_xlc2(fake))
    assert not fake.is_open


@pytest.mark.parametrize(
    "identity",
    ["EduceLab XLC2 (FW 1.2.0)", "EduceLab XLC2,1.0,1.2.0", "EduceLab XLC2,1.0,dev"],
    ids=["1.2.0 handshake", "1.2.0", "unparseable"],
)
def test_open_refuses_old_or_unknown_firmware(identity):
    fake = ScriptedSerial([("<?>", [identity])])
    light = make_xlc2(fake)
    with pytest.raises(DeviceError, match="firmware/xlc2") as raised:
        light.open()
    assert not isinstance(raised.value, DeviceNotFound)
    assert not light.is_open
    assert not fake.is_open


def test_open_fails_when_the_port_is_missing():
    with pytest.raises(DeviceNotFound):
        opened(make_xlc2(ScriptedSerial(present=False)))


# Levels


def test_channels_are_a_white_strip_and_a_940nm_array():
    white, ir = make_xlc2(ScriptedSerial()).channels
    assert (white.id, white.kind, white.wavelength_nm) == (
        "white",
        ChannelKind.MONO,
        None,
    )
    assert (ir.id, ir.kind, ir.wavelength_nm) == ("940nm", ChannelKind.MONO, 940.0)


def test_levels_go_to_the_strip_and_the_array():
    fake = scripted(("<B,128>", ["OK"]), ("<X,255>", ["OK"]), ("<X,3>", ["OK"]))
    light = opened(make_xlc2(fake))
    light.set_level("white", 0.5)
    light.set_level("940nm", 1.0)
    light.set_level("940nm", 0.01)
    fake.assert_done()


def test_levels_read_back_as_set():
    fake, firmware = emulated()
    light = opened(make_xlc2(fake))
    light.set_level("white", 0.25)
    light.set_level("940nm", 0.75)
    assert firmware.pixels == [64] * 222
    assert firmware.xl4 == 191
    assert (light.level("white"), light.level("940nm")) == (0.25, 0.75)


def test_every_0_to_255_duty_survives_a_round_trip_through_a_level():
    # pgs-scan's saved presets store integer duties, passed in as b / 255; a
    # change to the scaling must not change their light output.
    fake, firmware = emulated()
    light = opened(make_xlc2(fake))
    for duty in range(256):
        light.set_level("white", duty / 255)
        light.set_level("940nm", duty / 255)
        assert (firmware.pixels[0], firmware.xl4) == (duty, duty)


def test_channel_ids_are_stable():
    # Consumers store these in presets and metadata (docs/hardware/xlc2.md).
    assert [c.id for c in XLC2("COM5").channels] == ["white", "940nm"]


def test_all_off_turns_off_both_outputs():
    fake, firmware = emulated()
    light = opened(make_xlc2(fake))
    light.set_level("white", 1.0)
    light.set_level("940nm", 1.0)
    light.all_off()
    assert (firmware.pixels, firmware.xl4) == ([0] * 222, 0)


def test_err_raises_command_rejected():
    light = opened(make_xlc2(scripted(("<B,255>", ["ERR invalid command"]))))
    with pytest.raises(CommandRejected, match="invalid command"):
        light.set_level("white", 1.0)


def test_a_rejected_level_is_not_remembered():
    light = opened(make_xlc2(scripted(("<B,255>", ["ERR invalid command"]))))
    with pytest.raises(CommandRejected):
        light.set_level("white", 1.0)
    assert light.level("white") == 0.0


def test_silence_raises_device_timeout():
    light = opened(make_xlc2(scripted(("<B,255>", []))))
    with pytest.raises(DeviceTimeout):
        light.set_level("white", 1.0)


def test_a_pre_2_0_debug_line_raises_device_error():
    light = opened(make_xlc2(scripted(("<B,255>", ["Got a brightness command"]))))
    with pytest.raises(DeviceError, match="Got a brightness command"):
        light.set_level("white", 1.0)


def test_a_late_reply_is_not_mistaken_for_the_next_one():
    fake = scripted(("<B,255>", []), ("<X,255>", ["ERR invalid command"]))
    light = opened(make_xlc2(fake))
    with pytest.raises(DeviceTimeout):
        light.set_level("white", 1.0)
    fake.queue("OK")
    with pytest.raises(CommandRejected):
        light.set_level("940nm", 1.0)


# Closing


def test_close_turns_off_both_outputs():
    fake = scripted(("<B,0>", ["OK"]), ("<X,0>", ["OK"]))
    light = opened(make_xlc2(fake))
    light.close()
    fake.assert_done()
    assert not fake.is_open


@pytest.mark.parametrize("reply", [[], ["ERR invalid command"]], ids=["silent", "ERR"])
def test_close_turns_off_the_940nm_array_even_if_the_strip_fails(reply, caplog):
    fake = scripted(("<B,0>", reply), ("<X,0>", ["OK"]))
    light = opened(make_xlc2(fake))
    light.close()
    fake.assert_done()
    assert "could not turn off" in caplog.text


def test_close_after_unplugging_still_closes(caplog):
    fake = scripted()
    light = opened(make_xlc2(fake))
    fake.unplug()
    light.close()
    assert not light.is_open
    assert "could not turn off" in caplog.text


# Discovery and finding a board by serial number

CH340 = PortInfo(
    "/dev/cu.usbserial-110", vid=0x1A86, pid=0x7523, serial_number="XLC2-0001"
)
OTHER = PortInfo("/dev/cu.usbserial-120", vid=0x1A86, pid=0x7523, serial_number="A1")


def attach(monkeypatch, boards):
    """Attach ``boards``, a mapping of PortInfo to the ScriptedSerial behind it."""
    by_device = {port.device: fake for port, fake in boards.items()}
    monkeypatch.setattr(_serial, "list_ports", lambda: list(boards))
    monkeypatch.setattr(
        xlc,
        "_default_transport",
        lambda port: make_transport(by_device[port], port),
    )
    return boards


def test_discover_finds_nothing_without_touching_ports(monkeypatch):
    def no_listing():
        raise AssertionError("discover() listed ports")

    monkeypatch.setattr(_serial, "list_ports", no_listing)
    assert XLC2.discover() == []


def test_serial_number_picks_the_port_ignoring_case(monkeypatch):
    boards = attach(monkeypatch, {OTHER: ScriptedSerial(), CH340: emulated()[0]})
    with XLC2(serial_number="xlc2-0001") as light:
        assert light.info.address == CH340.device
        assert light.info.serial_number == "XLC2-0001"
    assert boards[OTHER].opened_with is None


def test_serial_number_must_match_exactly(monkeypatch):
    attach(monkeypatch, {CH340: emulated()[0]})
    with pytest.raises(DeviceNotFound, match="XLC2-000"):
        XLC2(serial_number="XLC2-000").open()


def test_reopening_by_serial_number_finds_a_replugged_board(monkeypatch):
    attach(monkeypatch, {CH340: emulated()[0]})
    light = XLC2(serial_number="XLC2-0001")
    light.open()
    light.close()
    replugged = PortInfo("/dev/cu.usbserial-140", serial_number="XLC2-0001")
    attach(monkeypatch, {replugged: emulated()[0]})
    light.open()
    assert light.info.address == replugged.device


def test_port_and_serial_number_are_exclusive():
    with pytest.raises(ValueError):
        XLC2("COM3", serial_number="XLC2-0001")


def test_open_without_a_port_or_serial_number_says_which_to_give():
    with pytest.raises(DeviceNotFound, match="port=.*serial_number="):
        XLC2().open()
