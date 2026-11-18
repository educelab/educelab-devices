import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from contracts import LightContract
from educelab.devices import _serial
from educelab.devices._serial import PortInfo, SerialTransport
from educelab.devices.errors import (
    CommandRejected,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    NotConnected,
)
from educelab.devices.lights import ChannelKind, xlc
from educelab.devices.lights.misha import NAMED_PANELS
from educelab.devices.lights.xlc import XLC2
from educelab.devices.stages.xcarve import XCONTROLLERS
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


def emulated(**firmware_options):
    """A board running emulated firmware, and the firmware."""
    firmware = XLCFirmware(**firmware_options)
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
    assert firmware("<U,XLC2-01>") == ["OK"]
    assert firmware("<?>") == ["EduceLab XLC2,1.0,2.1.0,XLC2-01"]
    for bad in ("<U>", "<U,>", "<U,a b>", "<U,a,b>", "<U,ABCDEFGHIJKLMNOPQ>"):
        assert firmware(bad) == ["ERR invalid command"], bad
    assert firmware.unit_id == "XLC2-01"


def test_firmware_emulation_plays_2_0_0():
    firmware = XLCFirmware("2.0.0", unit_id="ignored")
    assert firmware("<?>") == ["EduceLab XLC2,1.0,2.0.0"]
    assert firmware("<U,XLC2-01>") == ["ERR unknown command"]


class TestXLC2Contract(LightContract):
    @pytest.fixture(autouse=True)
    def no_ports(self, monkeypatch):
        monkeypatch.setattr(_serial, "list_ports", list)

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
    assert light.info.firmware_version == "2.1.0"
    assert light.info.serial_number is None
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


def test_open_reports_the_unit_id_as_the_serial_number():
    light = opened(make_xlc2(emulated(unit_id="XLC2-01")[0]))
    assert light.info.serial_number == "XLC2-01"
    assert "XLC2-01" in repr(light)


def test_2_0_0_firmware_opens_without_a_serial_number():
    light = opened(make_xlc2(emulated(firmware="2.0.0")[0]))
    assert light.info.firmware_version == "2.0.0"
    assert light.info.serial_number is None


def test_serial_number_is_unknown_before_opening_by_port():
    assert make_xlc2(emulated(unit_id="XLC2-01")[0]).info.serial_number is None


def test_requested_serial_number_is_reported_before_open():
    assert XLC2(serial_number="XLC2-01").info.serial_number == "XLC2-01"


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("EduceLab XLC2,1.0,2.0.0", ("1.0", "2.0.0", None)),
        ("EduceLab XLC2,1.0,2.1.0,", ("1.0", "2.1.0", None)),
        ("EduceLab XLC2,1.0,2.1.0, XLC2-01 ", ("1.0", "2.1.0", "XLC2-01")),
        ("EduceLab XLC2 (FW 1.2.0)", (None, "1.2.0", None)),
        ("EduceLab XLC2,1.0,2.1.0,a,b", None),
        ("EduceLab XLC2,1.0", None),
    ],
)
def test_handshake_replies_parse_with_or_without_a_unit_id(reply, expected):
    assert xlc._parse_identity(reply) == expected


# Setting the unit ID


def test_set_unit_id_stores_it_and_reports_it():
    fake, firmware = emulated()
    light = opened(make_xlc2(fake))
    light.set_unit_id("XLC2-07")
    assert firmware.unit_id == "XLC2-07"
    assert light.info.serial_number == "XLC2-07"


@pytest.mark.parametrize("unit_id", ["", "a b", "a,b", "é", "X" * 17])
def test_set_unit_id_refuses_an_invalid_id_without_sending_it(unit_id):
    fake, _ = emulated()
    light = opened(make_xlc2(fake))
    sent = len(fake.written)
    with pytest.raises(ValueError):
        light.set_unit_id(unit_id)
    assert len(fake.written) == sent


def test_set_unit_id_needs_2_1_0_firmware():
    fake, _ = emulated(firmware="2.0.0")
    light = opened(make_xlc2(fake))
    sent = len(fake.written)
    with pytest.raises(DeviceError, match="2.1.0"):
        light.set_unit_id("XLC2-01")
    assert len(fake.written) == sent


def test_set_unit_id_needs_an_open_board():
    with pytest.raises(NotConnected):
        make_xlc2(emulated()[0]).set_unit_id("XLC2-01")


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


# Discovery, probing and finding a board by its unit ID

HOLTEK = PortInfo("/dev/cu.usbmodem00001", vid=0x04D9, pid=0xB534, serial_number="0000")
CH340 = PortInfo("/dev/cu.usbserial-110", vid=0x1A86, pid=0x7523)
FTDI = PortInfo(
    "/dev/cu.usbserial-A1", vid=0x0403, pid=0x6001, serial_number="XCONTROLLER3"
)
OTHER_CH340 = PortInfo("/dev/cu.usbserial-120", vid=0x1A86, pid=0x7523)


def silent():
    return ScriptedSerial(respond=lambda line: [])


def untouchable():
    def refuse(line):
        raise AssertionError(f"{line!r} was sent to a port that isn't an XLC2's")

    fake = ScriptedSerial(respond=refuse)
    fake.open = refuse
    return fake


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


def test_discover_lists_xlc2_bridges_without_opening_them(monkeypatch):
    attach(
        monkeypatch,
        {HOLTEK: untouchable(), CH340: untouchable(), FTDI: untouchable()},
    )
    found = XLC2.discover()
    assert [(i.address, i.serial_number) for i in found] == [
        (HOLTEK.device, None),
        (CH340.device, None),
    ]
    assert {(i.backend, i.model) for i in found} == {("xlc", "XLC2")}


def test_probe_identifies_each_xlc2(monkeypatch):
    attach(
        monkeypatch,
        {
            HOLTEK: emulated(unit_id="XLC2-01")[0],
            CH340: emulated(firmware="2.0.0")[0],
        },
    )
    found = {i.address: i for i in XLC2.probe()}
    assert found[HOLTEK.device].serial_number == "XLC2-01"
    assert found[HOLTEK.device].firmware_version == "2.1.0"
    assert found[CH340.device].serial_number is None
    assert found[CH340.device].firmware_version == "2.0.0"


def test_probe_reports_old_firmware_so_it_can_be_reflashed(monkeypatch):
    fake, firmware = emulated()
    firmware.identity = "EduceLab XLC2 (FW 1.2.0)"
    attach(monkeypatch, {HOLTEK: fake})
    assert [i.firmware_version for i in XLC2.probe()] == ["1.2.0"]


def test_probe_closes_every_port(monkeypatch):
    boards = attach(monkeypatch, {HOLTEK: emulated()[0], CH340: silent()})
    XLC2.probe()
    assert not any(fake.is_open for fake in boards.values())
    assert all(fake.opened_with for fake in boards.values())


def test_probe_never_opens_ports_off_the_allowlist(monkeypatch):
    attach(monkeypatch, {FTDI: untouchable(), HOLTEK: emulated()[0]})
    assert [i.address for i in XLC2.probe()] == [HOLTEK.device]


def _ids(value):
    return {value} if isinstance(value, int) else set(value)


@pytest.mark.parametrize(
    "other", [XCONTROLLERS, NAMED_PANELS], ids=["X-Carve", "MISHA"]
)
def test_probed_bridges_never_include_another_drivers_ports(other):
    # Probing resets the board behind a port, so the allowlist must stay clear
    # of every other serial driver's ports (docs/hardware/xlc2.md).
    for bridge in xlc.BRIDGES:
        assert not (_ids(bridge.vid) & _ids(other.vid))


def test_probe_skips_busy_and_silent_ports(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger=xlc.__name__)
    attach(
        monkeypatch,
        {
            CH340: ScriptedSerial(present=False),
            OTHER_CH340: silent(),
            HOLTEK: emulated(unit_id="XLC2-01")[0],
        },
    )
    assert [i.serial_number for i in XLC2.probe()] == ["XLC2-01"]
    assert CH340.device in caplog.text and OTHER_CH340.device in caplog.text


def test_an_unexpected_probe_failure_leaves_no_port_open(monkeypatch):
    def broken(line):
        raise RuntimeError("driver bug")

    boards = attach(
        monkeypatch,
        {CH340: ScriptedSerial(respond=broken), HOLTEK: emulated()[0]},
    )
    with pytest.raises(RuntimeError, match="driver bug"):
        XLC2.probe()
    assert not any(fake.is_open for fake in boards.values())


def test_an_interrupted_probe_leaves_no_port_open(monkeypatch):
    class Interrupted(ThreadPoolExecutor):
        def __exit__(self, *exc):
            super().__exit__(*exc)
            raise KeyboardInterrupt

    monkeypatch.setattr(xlc, "ThreadPoolExecutor", Interrupted)
    boards = attach(monkeypatch, {CH340: emulated()[0], HOLTEK: emulated()[0]})
    with pytest.raises(KeyboardInterrupt):
        XLC2.probe()
    assert not any(fake.is_open for fake in boards.values())


def test_an_interrupt_in_one_probe_leaves_no_port_open(monkeypatch):
    def interrupted(line):
        raise KeyboardInterrupt

    boards = attach(
        monkeypatch,
        {CH340: ScriptedSerial(respond=interrupted), HOLTEK: emulated()[0]},
    )
    with pytest.raises(KeyboardInterrupt):
        XLC2.probe()
    assert not any(fake.is_open for fake in boards.values())


def test_probe_waits_for_every_port_at_once(monkeypatch):
    monkeypatch.setattr(xlc, "HANDSHAKE_TIMEOUT_S", 0.3)
    ports = [
        PortInfo(f"/dev/cu.usbserial-{n}", vid=0x1A86, pid=0x7523) for n in range(4)
    ]
    attach(monkeypatch, {port: silent() for port in ports})
    start = time.monotonic()
    assert XLC2.probe() == []
    assert time.monotonic() - start < 0.9


def test_serial_number_opens_the_board_with_that_unit_id(monkeypatch):
    boards = attach(
        monkeypatch,
        {
            CH340: emulated(unit_id="XLC2-02")[0],
            HOLTEK: emulated(unit_id="XLC2-01")[0],
        },
    )
    with XLC2(serial_number="XLC2-01") as light:
        assert light.info.address == HOLTEK.device
        assert light.info.serial_number == "XLC2-01"
        light.set_level("white", 1.0)
        assert boards[CH340].is_open is False
    # Opened once: the probe's connection was kept, not reopened.
    assert boards[HOLTEK].written.count("<?>") == 1


def test_serial_number_is_matched_exactly(monkeypatch):
    attach(monkeypatch, {HOLTEK: emulated(unit_id="XLC2-01")[0]})
    with pytest.raises(DeviceNotFound, match=r"xlc2-01.*XLC2-01"):
        XLC2(serial_number="xlc2-01").open()


def test_serial_number_shared_by_two_boards_is_refused(monkeypatch):
    boards = attach(
        monkeypatch,
        {
            CH340: emulated(unit_id="XLC2-01")[0],
            HOLTEK: emulated(unit_id="XLC2-01")[0],
        },
    )
    with pytest.raises(DeviceError, match="2 XLC2s"):
        XLC2(serial_number="XLC2-01").open()
    assert not any(fake.is_open for fake in boards.values())


def test_reopening_finds_the_board_by_its_new_unit_id(monkeypatch):
    attach(monkeypatch, {HOLTEK: emulated(unit_id="XLC2-01")[0]})
    light = XLC2(serial_number="XLC2-01")
    light.open()
    light.set_unit_id("XLC2-05")
    light.close()
    light.open()
    assert light.info.serial_number == "XLC2-05"


def test_reopening_by_serial_number_finds_a_replugged_board(monkeypatch):
    attach(monkeypatch, {HOLTEK: emulated(unit_id="XLC2-01")[0]})
    light = XLC2(serial_number="XLC2-01")
    light.open()
    light.close()
    replugged = PortInfo("/dev/cu.usbmodem00002", vid=0x04D9, pid=0xB534)
    attach(monkeypatch, {replugged: emulated(unit_id="XLC2-01")[0]})
    light.open()
    assert light.info.address == replugged.device


def test_port_and_serial_number_are_exclusive():
    with pytest.raises(ValueError):
        XLC2("COM3", serial_number="XLC2-0001")


def test_open_without_a_port_or_serial_number_says_which_to_give():
    with pytest.raises(DeviceNotFound, match="port=.*serial_number="):
        XLC2().open()
