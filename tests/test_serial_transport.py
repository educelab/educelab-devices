import sys

import pytest

from educelab.devices import _serial


def test_missing_pyserial_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "serial", None)
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
