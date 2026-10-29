import sys
import time

import pytest

from educelab.devices import _serial
from educelab.devices.errors import (
    CommandRejected,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    NotConnected,
)


def test_missing_pyserial_names_the_extra(monkeypatch):
    # A submodule already imported by an earlier test is found without its parent.
    for name in ("serial", "serial.tools", "serial.tools.list_ports"):
        monkeypatch.setitem(sys.modules, name, None)
    with pytest.raises(ImportError, match=r"educelab-devices\[serial\]"):
        _serial.list_ports()


NANO = _serial.PortInfo(
    "/dev/ttyUSB0", vid=0x1A86, pid=0x7523, description="USB Serial"
)
XCONTROLLER = _serial.PortInfo(
    "/dev/ttyACM0",
    vid=0x2341,
    pid=0x0043,
    serial_number="XCONTROLLER-1234",
    description="Arduino Uno",
)
NOT_USB = _serial.PortInfo("/dev/ttyS0")
PORTS = [NANO, XCONTROLLER, NOT_USB]


def test_no_criteria_matches_nothing():
    assert _serial.PortMatch().matching(PORTS) == []


def test_every_given_criterion_must_match():
    # pgs-scan #84: a port matching only the VID was accepted.
    match = _serial.PortMatch(vid=0x1A86, pid=0x0043)
    assert match.matching(PORTS) == []


def test_matches_on_all_criteria():
    match = _serial.PortMatch(
        vid=0x2341, pid=0x0043, serial_number="XCONTROLLER", description="uno"
    )
    assert match.matching(PORTS) == [XCONTROLLER]


def test_ids_accept_alternatives():
    match = _serial.PortMatch(vid={0x1A86, 0x2341})
    assert match.matching(PORTS) == [NANO, XCONTROLLER]


def test_text_criteria_match_case_insensitive_substrings():
    assert _serial.PortMatch(serial_number="xcontroller").matching(PORTS) == [
        XCONTROLLER
    ]
    assert _serial.PortMatch(description="usb serial").matching(PORTS) == [NANO]


def test_missing_port_attribute_never_matches():
    assert _serial.PortMatch(serial_number="1234").matching([NANO, NOT_USB]) == []


def test_find_ports_lists_host_ports_by_default(monkeypatch):
    monkeypatch.setattr(_serial, "list_ports", lambda: PORTS)
    assert _serial.find_ports(_serial.PortMatch(vid=0x1A86)) == [NANO]


class StubSerial:
    """The slice of ``serial.Serial`` the transport uses, fed canned bytes."""

    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.written = []
        self.timeout = None
        self.closed = False
        self.flushed_input = 0

    def reset_input_buffer(self):
        self.flushed_input += 1

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass

    def readline(self):
        if self.chunks:
            return self.chunks.pop(0)
        time.sleep(self.timeout)
        return b""

    def close(self):
        self.closed = True


def open_transport(stub, **kwargs):
    transport = _serial.SerialTransport(
        "/dev/ttyFAKE", baudrate=9600, opener=lambda *args: stub, **kwargs
    )
    transport.open()
    return transport


def test_open_passes_port_settings_to_the_opener():
    calls = []

    def opener(port, baudrate, timeout):
        calls.append((port, baudrate, timeout))
        return StubSerial()

    transport = _serial.SerialTransport(
        "COM3", baudrate=9600, timeout=0.5, opener=opener
    )
    transport.open()
    assert calls == [("COM3", 9600, 0.5)]
    assert transport.is_open


def test_open_waits_out_the_reset_then_discards_boot_output(monkeypatch):
    slept = []
    monkeypatch.setattr(_serial.time, "sleep", slept.append)
    stub = StubSerial()
    open_transport(stub, reset_settle=2.0)
    assert slept == [2.0]
    assert stub.flushed_input == 1


def test_open_failure_raises_device_not_found():
    def opener(*args):
        raise OSError("no such port")

    transport = _serial.SerialTransport("COM9", baudrate=9600, opener=opener)
    with pytest.raises(DeviceNotFound, match="COM9"):
        transport.open()
    assert not transport.is_open


def test_write_line_appends_newline():
    stub = StubSerial()
    open_transport(stub).write_line("365,50")
    assert stub.written == [b"365,50\n"]


def test_write_bytes_sends_them_as_they_are():
    # GRBL's real-time commands are single bytes with no line ending.
    stub = StubSerial()
    open_transport(stub).write_bytes(b"?\x85")
    assert stub.written == [b"?\x85"]


def test_write_bytes_after_unplugging_raises_not_connected():
    stub = StubSerial()
    transport = open_transport(stub)

    def unplugged(data):
        raise OSError("device disconnected")

    stub.write = unplugged
    with pytest.raises(NotConnected):
        transport.write_bytes(b"!")
    assert not transport.is_open


def test_read_line_strips_and_skips_blank_lines():
    stub = StubSerial([b"\r\n", b"  OK\r\n"])
    assert open_transport(stub).read_line() == "OK"


def test_read_line_joins_a_line_split_by_a_timeout():
    stub = StubSerial([b"MISHA LED", b" Panels,1.1,2.0.0\n"])
    assert open_transport(stub).read_line() == "MISHA LED Panels,1.1,2.0.0"


def test_read_line_times_out():
    transport = open_transport(StubSerial())
    start = time.monotonic()
    with pytest.raises(DeviceTimeout, match="ttyFAKE"):
        transport.read_line(timeout=0.05)
    assert time.monotonic() - start < 1.0


def test_io_error_raises_not_connected():
    stub = StubSerial()
    transport = open_transport(stub)

    def unplugged(data):
        raise OSError("device disconnected")

    stub.write = unplugged
    with pytest.raises(NotConnected):
        transport.write_line("0,0")


def test_closed_transport_raises_not_connected():
    stub = StubSerial()
    transport = open_transport(stub)
    transport.close()
    transport.close()
    assert stub.closed
    with pytest.raises(NotConnected):
        transport.write_line("0,0")


def test_handshake_finds_the_reply_behind_chatter():
    stub = StubSerial([b"booting\n", b"MISHA LED Panels,1.1,2.0.0\n"])
    transport = open_transport(stub)
    reply = _serial.handshake(
        transport, "ID", lambda line: line if line.startswith("MISHA") else None
    )
    assert reply == "MISHA LED Panels,1.1,2.0.0"
    assert stub.written == [b"ID\n"]


def test_handshake_without_an_answer_returns_none():
    transport = open_transport(StubSerial([b"$GPGGA,123519\n"]))
    assert _serial.handshake(transport, "ID", lambda line: None, timeout=0.05) is None


class CountingSerial(StubSerial):
    """A StubSerial counting timeout changes, each a reconfigure in pyserial."""

    def __init__(self):
        self.timeout_changes = 0
        super().__init__()

    def __setattr__(self, name, value):
        if name == "timeout" and getattr(self, "timeout", None) != value:
            self.timeout_changes += 1
        super().__setattr__(name, value)


def test_repeated_reads_with_one_timeout_set_it_once():
    # A reader thread polls read_line(0.1) for the life of the connection.
    stub = CountingSerial()
    transport = open_transport(stub)
    before = stub.timeout_changes
    for _ in range(5):
        with pytest.raises(DeviceTimeout):
            transport.read_line(0.01)
    assert stub.timeout_changes - before == 1


def test_wait_for_skips_lines_until_one_parses():
    transport = open_transport(StubSerial([b"noise\n", b"Grbl 1.1g\n"]))
    reply = _serial.wait_for(
        transport, lambda line: line[5:] if line.startswith("Grbl") else None
    )
    assert reply == "1.1g"
    assert _serial.wait_for(transport, lambda line: line, timeout=0.02) is None


@pytest.mark.parametrize(
    ("reply", "raised", "match"),
    [
        (b"ERR bad index\n", CommandRejected, "bad index"),
        (b"ERR\n", CommandRejected, "no reason given"),
        (b"what?\n", DeviceError, "what"),
    ],
)
def test_command_raises_unless_the_reply_is_ok(reply, raised, match):
    transport = open_transport(StubSerial([reply]))
    with pytest.raises(raised, match=match):
        _serial.command(transport, "B,1", "<board>")


def test_command_returns_what_follows_ok():
    stub = StubSerial([b"OK 128\n"])
    assert _serial.command(open_transport(stub), "B?", "<board>") == "128"
    assert stub.written == [b"B?\n"]


def test_parse_version_sorts_unparseable_text_first():
    assert _serial.parse_version("2.10.0") > _serial.parse_version("2.9.1")
    assert _serial.parse_version("dev") < _serial.parse_version("0.0.1")


A = _serial.PortInfo("/dev/ttyA", serial_number="Panel-A")
B = _serial.PortInfo("/dev/ttyB", serial_number="PANEL-AB")


def test_port_with_serial_number_matches_exactly_ignoring_case(monkeypatch):
    monkeypatch.setattr(_serial, "list_ports", lambda: [B, A])
    assert _serial.port_with_serial_number("panel-a") == A
    with pytest.raises(DeviceNotFound, match="PANEL"):
        _serial.port_with_serial_number("PANEL")


def test_only_port_needs_exactly_one_match(monkeypatch):
    match = _serial.PortMatch(serial_number="panel")
    names = {"name": "panel", "plural": "panels", "hint": "Plug one in."}
    monkeypatch.setattr(_serial, "list_ports", lambda: [A])
    assert _serial.only_port(match, **names) == A
    monkeypatch.setattr(_serial, "list_ports", lambda: [A, B])
    with pytest.raises(DeviceError, match="several panels.*Panel-A at /dev/ttyA"):
        _serial.only_port(match, **names)
    monkeypatch.setattr(_serial, "list_ports", list)
    with pytest.raises(DeviceNotFound, match="no panel is attached. Plug one in."):
        _serial.only_port(match, **names)
