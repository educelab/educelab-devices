import pytest

from educelab.devices._serial import SerialTransport
from educelab.devices.errors import DeviceNotFound, DeviceTimeout, NotConnected
from fakes.serial_port import ScriptedSerial


def connect(fake, timeout=0.05):
    transport = SerialTransport(
        "/dev/ttyFAKE", baudrate=9600, timeout=timeout, opener=fake.open
    )
    transport.open()
    return transport


def test_script_replies_to_expected_writes_in_order():
    fake = ScriptedSerial([("ID", ["MISHA LED Panels,1.1,2.0.0"]), ("365,50", ["OK"])])
    transport = connect(fake)
    transport.write_line("ID")
    assert transport.read_line() == "MISHA LED Panels,1.1,2.0.0"
    transport.write_line("365,50")
    assert transport.read_line() == "OK"
    fake.assert_done()
    assert fake.written == ["ID", "365,50"]


def test_unexpected_write_fails_the_test():
    transport = connect(ScriptedSerial([("ID", [])]))
    with pytest.raises(AssertionError, match="expected 'ID'"):
        transport.write_line("0,0")


def test_unfinished_script_fails_assert_done():
    fake = ScriptedSerial([("ID", [])])
    connect(fake)
    with pytest.raises(AssertionError, match="ID"):
        fake.assert_done()


def test_responder_answers_writes_after_the_script():
    fake = ScriptedSerial([("ID", ["X,1,2"])], respond=lambda line: ["OK"])
    transport = connect(fake)
    transport.write_line("ID")
    transport.read_line()
    transport.write_line("anything")
    assert transport.read_line() == "OK"


def test_silence_times_out():
    transport = connect(ScriptedSerial([("ID", [])]))
    transport.write_line("ID")
    with pytest.raises(DeviceTimeout):
        transport.read_line()


def test_reset_discards_queued_lines():
    fake = ScriptedSerial()
    fake.queue("booting")
    transport = connect(fake)
    with pytest.raises(DeviceTimeout):
        transport.read_line()


def test_records_how_the_port_was_opened():
    fake = ScriptedSerial()
    connect(fake)
    assert fake.opened_with == ("/dev/ttyFAKE", 9600, 0.05)
    assert fake.is_open


def test_missing_port_fails_to_open():
    with pytest.raises(DeviceNotFound):
        connect(ScriptedSerial(present=False))


def test_unplugging_loses_the_connection():
    fake = ScriptedSerial()
    transport = connect(fake)
    fake.unplug()
    with pytest.raises(NotConnected):
        transport.write_line("0,0")
    assert not fake.is_open
