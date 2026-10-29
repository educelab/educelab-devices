import pytest

from contracts import LightContract
from educelab.devices._serial import PortInfo, SerialTransport
from educelab.devices.errors import (
    CommandRejected,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
)
from educelab.devices.lights import ChannelKind, xlc
from educelab.devices.lights.xlc import XLC1, XLC2
from fakes.serial_port import ScriptedSerial
from fakes.xlc import XLC1_IDENTITY, XLC2_IDENTITY, XLCFirmware


@pytest.fixture(autouse=True)
def quick_handshake(monkeypatch):
    monkeypatch.setattr(xlc, "HANDSHAKE_TIMEOUT_S", 0.05)


def make_transport(fake, port="/dev/ttyXLC"):
    return SerialTransport(port, baudrate=9600, timeout=0.05, opener=fake.open)


def make_xlc1(fake, port="/dev/ttyXLC"):
    return XLC1(port, transport=make_transport(fake, port))


def make_xlc2(fake, port="/dev/ttyXLC"):
    return XLC2(port, transport=make_transport(fake, port))


def opened(light):
    light.open()
    return light


def xlc1_scripted(*steps):
    """An XLC1 that identifies itself and is turned off on open, then ``steps``."""
    return ScriptedSerial([("<?>", [XLC1_IDENTITY]), ("<C,0,0,0>", ["OK"]), *steps])


def xlc2_scripted(*steps):
    """An XLC2 that identifies itself and is turned off on open, then ``steps``."""
    return ScriptedSerial(
        [("<?>", [XLC2_IDENTITY]), ("<B,0>", ["OK"]), ("<X,0>", ["OK"]), *steps]
    )


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
    xlc2 = XLCFirmware("XLC2")
    assert xlc2("<C,1,2,3>") == ["ERR unknown command"]
    assert xlc2("<L,222,9>") == ["ERR index out of range"]
    assert xlc2("<S,1,2,3,4>") == ["OK"]
    assert xlc2.pixels[54:56] == [1, 2]


class TestXLC1Contract(LightContract):
    @pytest.fixture
    def make_device(self):
        return lambda: make_xlc1(emulated()[0])


class TestXLC2Contract(LightContract):
    @pytest.fixture
    def make_device(self):
        return lambda: make_xlc2(emulated("XLC2")[0])


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


# XLC2


def test_xlc2_open_identifies_the_board_and_turns_both_outputs_off():
    fake = xlc2_scripted()
    light = opened(make_xlc2(fake))
    fake.assert_done()
    assert light.info.model == "XLC2"
    assert light.info.firmware_version == "2.0.0"
    assert light.hardware_version == "1.0"
    assert light.level("white") == 0.0
    assert light.level("940nm") == 0.0


def test_xlc2_has_a_white_strip_and_a_940nm_array():
    white, ir = make_xlc2(ScriptedSerial()).channels
    assert (white.id, white.kind, white.wavelength_nm) == ("white", "mono", None)
    assert (ir.id, ir.kind, ir.wavelength_nm) == ("940nm", "mono", 940.0)


def test_xlc2_levels_go_to_the_strip_and_the_array():
    fake = xlc2_scripted(("<B,128>", ["OK"]), ("<X,255>", ["OK"]), ("<X,3>", ["OK"]))
    light = opened(make_xlc2(fake))
    light.set_level("white", 0.5)
    light.set_level("940nm", 1.0)
    light.set_level("940nm", 0.01)
    fake.assert_done()


def test_xlc2_levels_read_back_as_set():
    fake, firmware = emulated("XLC2")
    light = opened(make_xlc2(fake))
    light.set_level("white", 0.25)
    light.set_level("940nm", 0.75)
    assert firmware.pixels == [64] * 222
    assert firmware.xl4 == 191
    assert (light.level("white"), light.level("940nm")) == (0.25, 0.75)


def test_xlc2_all_off_turns_off_both_outputs():
    fake, firmware = emulated("XLC2")
    light = opened(make_xlc2(fake))
    light.set_level("white", 1.0)
    light.set_level("940nm", 1.0)
    light.all_off()
    assert (firmware.pixels, firmware.xl4) == ([0] * 222, 0)


def test_xlc2_close_turns_off_both_outputs():
    fake = xlc2_scripted(("<B,0>", ["OK"]), ("<X,0>", ["OK"]))
    opened(make_xlc2(fake)).close()
    fake.assert_done()


@pytest.mark.parametrize(
    "identity",
    ["EduceLab XLC2 (FW 1.2.0)", "EduceLab XLC2,1.0,1.2.0"],
    ids=["1.2.0 handshake", "1.2.0"],
)
def test_xlc2_open_refuses_old_firmware(identity):
    fake = ScriptedSerial([("<?>", [identity])])
    with pytest.raises(DeviceError, match="firmware/xlc2"):
        opened(make_xlc2(fake))
    assert not fake.is_open


def test_xlc2_does_not_open_an_xlc1():
    fake = ScriptedSerial([("<?>", [XLC1_IDENTITY])])
    with pytest.raises(DeviceNotFound):
        opened(make_xlc2(fake))


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


# Discovery and finding a board by serial number

CH340 = PortInfo(
    "/dev/cu.usbserial-110", vid=0x1A86, pid=0x7523, serial_number="XLC2-0001"
)
OTHER = PortInfo("/dev/cu.usbserial-120", vid=0x1A86, pid=0x7523, serial_number="A1")


def attach(monkeypatch, boards):
    """Attach ``boards``, a mapping of PortInfo to the ScriptedSerial behind it."""
    by_device = {port.device: fake for port, fake in boards.items()}
    monkeypatch.setattr(xlc, "list_ports", lambda: list(boards))
    monkeypatch.setattr(
        xlc,
        "_default_transport",
        lambda port: make_transport(by_device[port], port),
    )
    return boards


@pytest.mark.parametrize("cls", [XLC1, XLC2])
def test_discover_finds_nothing_without_touching_ports(monkeypatch, cls):
    def no_listing():
        raise AssertionError("discover() listed ports")

    monkeypatch.setattr(xlc, "list_ports", no_listing)
    assert cls.discover() == []


def test_serial_number_picks_the_port_ignoring_case(monkeypatch):
    boards = attach(monkeypatch, {OTHER: ScriptedSerial(), CH340: emulated("XLC2")[0]})
    with XLC2(serial_number="xlc2-0001") as light:
        assert light.info.address == CH340.device
        assert light.info.serial_number == "XLC2-0001"
    assert boards[OTHER].opened_with is None


def test_serial_number_must_match_exactly(monkeypatch):
    attach(monkeypatch, {CH340: emulated("XLC2")[0]})
    with pytest.raises(DeviceNotFound, match="XLC2-000"):
        XLC2(serial_number="XLC2-000").open()


def test_reopening_by_serial_number_finds_a_replugged_board(monkeypatch):
    attach(monkeypatch, {CH340: emulated("XLC2")[0]})
    light = XLC2(serial_number="XLC2-0001")
    light.open()
    light.close()
    replugged = PortInfo("/dev/cu.usbserial-140", serial_number="XLC2-0001")
    attach(monkeypatch, {replugged: emulated("XLC2")[0]})
    light.open()
    assert light.info.address == replugged.device


def test_port_and_serial_number_are_exclusive():
    with pytest.raises(ValueError):
        XLC1("COM3", serial_number="XLC2-0001")


def test_open_without_a_port_or_serial_number_says_which_to_give():
    with pytest.raises(DeviceNotFound, match="port=.*serial_number="):
        XLC1().open()
