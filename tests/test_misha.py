import pytest

from contracts import LightContract
from educelab.devices._serial import PortInfo, SerialTransport
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import (
    CommandRejected,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
)
from educelab.devices.lights import misha
from educelab.devices.lights.misha import MishaLight
from fakes.misha import IDENTITY, MishaFirmware
from fakes.serial_port import ScriptedSerial


@pytest.fixture(autouse=True)
def quick_handshake(monkeypatch):
    monkeypatch.setattr(misha, "HANDSHAKE_TIMEOUT_S", 0.05)


def make_transport(fake, port="/dev/ttyMISHA"):
    return SerialTransport(port, baudrate=9600, timeout=0.05, opener=fake.open)


def make_light(fake, port="/dev/ttyMISHA"):
    return MishaLight(port, transport=make_transport(fake, port))


def opened(fake):
    light = make_light(fake)
    light.open()
    return light


def scripted(*steps):
    """A panel that opens with everything off, then expects ``steps``."""
    return ScriptedSerial([("ID", [IDENTITY]), ("STATE", ["OK 0,0"]), *steps])


def emulated(firmware=None):
    """A panel running emulated firmware, and the firmware."""
    firmware = firmware or MishaFirmware()
    return ScriptedSerial(respond=firmware), firmware


class TestMishaContract(LightContract):
    @pytest.fixture(autouse=True)
    def no_ports(self, monkeypatch):
        monkeypatch.setattr(misha, "list_ports", list)

    @pytest.fixture
    def make_device(self):
        return lambda: make_light(emulated()[0])


def test_firmware_emulation_matches_the_documented_protocol():
    firmware = MishaFirmware()
    assert firmware("600,50") == ["ERR unknown wavelength"]
    assert firmware("590,101") == ["ERR intensity out of range"]
    assert firmware("590,") == ["ERR invalid command"]
    firmware("590,50")
    firmware("420,0")
    assert firmware("STATE") == ["OK 590,50"]
    firmware("OFF")
    assert firmware("STATE") == ["OK 0,0"]


# Opening


def test_open_identifies_the_board_and_reads_its_state():
    fake = scripted()
    light = opened(fake)
    fake.assert_done()
    assert light.info.firmware_version == "2.0.0"
    assert light.info.address == "/dev/ttyMISHA"
    assert light.hardware_version == "1.1"


def test_open_leaves_a_lit_band_on():
    fake, firmware = emulated()
    firmware.lit = (450, 50)
    light = opened(fake)
    assert light.level("450nm") == 0.5
    assert fake.written == ["ID", "STATE", "STATE"]


def test_open_skips_chatter_and_ignores_name_case():
    fake = ScriptedSerial(
        [("ID", ["booting", " misha led panels , 1.1 , 2.1.0 "]), ("STATE", ["OK 0,0"])]
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


@pytest.mark.parametrize("state", ["OK banana", "OK 600,50", "OK 450,150", "OK"])
def test_open_rejects_a_state_it_cannot_read(state):
    fake = ScriptedSerial([("ID", [IDENTITY]), ("STATE", [state])])
    with pytest.raises(DeviceError, match="state"):
        opened(fake)
    assert not fake.is_open


def test_open_fails_when_the_port_is_missing():
    with pytest.raises(DeviceNotFound):
        opened(ScriptedSerial(present=False))


# Levels


def test_channels_are_the_sixteen_bands():
    light = make_light(ScriptedSerial())
    assert [c.id for c in light.channels][:3] == ["365nm", "385nm", "395nm"]
    assert len(light.channels) == 16
    assert light.channels[-1].wavelength_nm == 940.0


def test_set_level_sends_percent_and_waits_for_ok():
    fake = scripted(("450,50", ["OK"]), ("940,26", ["OK"]), ("365,0", ["OK"]))
    light = opened(fake)
    light.set_level("450nm", 0.5)
    light.set_level("940nm", 0.256)
    light.set_level("365nm", 0.0)
    fake.assert_done()


def test_level_reads_the_panel():
    fake, firmware = emulated()
    light = opened(fake)
    light.set_level("940nm", 0.256)
    assert light.level("940nm") == 0.26
    firmware.lit = (365, 10)
    assert light.level("365nm") == 0.1
    assert light.level("940nm") == 0.0


def test_lighting_a_band_turns_the_others_off():
    light = opened(emulated()[0])
    light.set_level("450nm", 0.5)
    light.set_level("365nm", 1.0)
    assert light.level("365nm") == 1.0
    assert light.level("450nm") == 0.0


def test_turning_off_an_unlit_band_leaves_the_lit_one_alone():
    light = opened(emulated()[0])
    light.set_level("450nm", 0.5)
    light.set_level("365nm", 0.0)
    assert light.level("450nm") == 0.5
    light.set_level("450nm", 0.0)
    assert light.level("450nm") == 0.0


def test_all_off_sends_off():
    fake = scripted(("OFF", ["OK"]))
    opened(fake).all_off()
    fake.assert_done()


def test_err_raises_command_rejected():
    light = opened(scripted(("365,100", ["ERR unknown wavelength"])))
    with pytest.raises(CommandRejected, match="unknown wavelength"):
        light.set_level("365nm", 1.0)


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
    assert fake.written == ["ID", "STATE"]


# Closing


def test_close_turns_everything_off():
    fake = scripted(("OFF", ["OK"]))
    light = opened(fake)
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


# Discovery


def fake_ports(monkeypatch, boards):
    """Attach ``boards``, a mapping of PortInfo to the ScriptedSerial behind it."""
    by_device = {port.device: fake for port, fake in boards.items()}
    monkeypatch.setattr(misha, "list_ports", lambda: list(boards))
    monkeypatch.setattr(
        misha,
        "_default_transport",
        lambda port: make_transport(by_device[port], port),
    )


def test_discover_reports_boards_that_identify(monkeypatch):
    board = PortInfo("COM3", vid=0x1A86, pid=0x7523, serial_number="A1")
    old = PortInfo("COM4", vid=0x2341, pid=0x0043)
    fake_ports(
        monkeypatch,
        {
            board: ScriptedSerial([("ID", [IDENTITY])]),
            old: ScriptedSerial([("ID", ["MISHA LED Panels,1.1,1.0.0"])]),
        },
    )
    assert MishaLight.discover() == [
        DeviceInfo("misha", "MISHA", "COM3", "A1", "2.0.0"),
        DeviceInfo("misha", "MISHA", "COM4", None, "1.0.0"),
    ]


def test_discover_skips_strangers_and_busy_ports(monkeypatch):
    silent = ScriptedSerial([("ID", [])])
    fake_ports(
        monkeypatch,
        {
            PortInfo("COM5", vid=0x0403): silent,
            PortInfo("COM6", vid=0x10C4): ScriptedSerial([("ID", ["XLC2,1,1"])]),
            PortInfo("COM7", vid=0x1A86): ScriptedSerial(present=False),
        },
    )
    assert MishaLight.discover() == []
    assert not silent.is_open


def test_discover_never_writes_to_unknown_usb_devices(monkeypatch):
    webcam = ScriptedSerial()
    fake_ports(
        monkeypatch,
        {
            PortInfo("/dev/cu.usbmodem1", vid=0x291A): webcam,
            PortInfo("/dev/ttyS0"): ScriptedSerial(),
        },
    )
    assert MishaLight.discover() == []
    assert webcam.opened_with is None


# Finding the panel without a port


def test_open_without_a_port_uses_the_first_misha_found(monkeypatch):
    stranger = ScriptedSerial([("ID", ["XLC2,1,1"])])
    webcam = ScriptedSerial()
    panel = ScriptedSerial(respond=MishaFirmware())
    fake_ports(
        monkeypatch,
        {
            PortInfo("COM2", vid=0x291A): webcam,
            PortInfo("COM3", vid=0x0403): stranger,
            PortInfo("COM4", vid=0x1A86): panel,
        },
    )
    light = MishaLight()
    assert light.info.address is None
    light.open()
    assert light.info.address == "COM4"
    assert light.info.firmware_version == "2.0.0"
    assert "COM4" in repr(light)
    assert panel.written == ["ID", "STATE"]
    assert not stranger.is_open
    assert webcam.opened_with is None


def test_open_without_a_port_fails_when_no_misha_answers(monkeypatch):
    fake_ports(
        monkeypatch, {PortInfo("COM3", vid=0x0403): ScriptedSerial(present=False)}
    )
    with pytest.raises(DeviceNotFound, match="port="):
        MishaLight().open()


def test_open_without_a_port_refuses_old_firmware(monkeypatch):
    old = ScriptedSerial([("ID", ["MISHA LED Panels,1.1,1.0.0"])])
    fake_ports(monkeypatch, {PortInfo("COM3", vid=0x1A86): old})
    with pytest.raises(DeviceError, match="firmware/misha"):
        MishaLight().open()
    assert not old.is_open


def test_reopening_without_a_port_searches_again(monkeypatch):
    first = ScriptedSerial(respond=MishaFirmware())
    fake_ports(monkeypatch, {PortInfo("COM3", vid=0x1A86): first})
    light = MishaLight()
    light.open()
    light.close()
    replugged = ScriptedSerial(respond=MishaFirmware())
    fake_ports(monkeypatch, {PortInfo("COM7", vid=0x1A86): replugged})
    light.open()
    assert light.info.address == "COM7"
