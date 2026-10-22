"""The GenTL driver's node-map code against real ``genapi``, without a producer.

Needs the ``genicam`` package (``[gentl]``); skips without it. A hand-written
camera description is served through a stand-in GenTL port, so this checks the
driver's XML loading, port adapter, and node access against the real library.
"""

import io
import struct
import zipfile

import pytest

from educelab.devices.cameras import gentl
from educelab.devices.errors import DeviceError

XML = """<?xml version="1.0" encoding="utf-8"?>
<RegisterDescription ModelName="Probe" VendorName="EduceLab" StandardNameSpace="None"
  SchemaMajorVersion="1" SchemaMinorVersion="1" SchemaSubMinorVersion="0"
  MajorVersion="1" MinorVersion="0" SubMinorVersion="0" ToolTip=""
  ProductGuid="00000000-0000-0000-0000-000000000000"
  VersionGuid="00000000-0000-0000-0000-000000000001"
  xmlns="http://www.genicam.org/GenApi/Version_1_1">
  <Category Name="Root">
    <pFeature>DeviceModelName</pFeature><pFeature>ExposureTime</pFeature>
    <pFeature>ExposureAuto</pFeature><pFeature>Width</pFeature>
    <pFeature>TimestampLatch</pFeature>
  </Category>
  <StringReg Name="DeviceModelName">
    <Address>0x40</Address><Length>16</Length><AccessMode>RO</AccessMode>
    <pPort>Device</pPort>
  </StringReg>
  <Float Name="ExposureTime">
    <pIsLocked>AutoOn</pIsLocked><pValue>ExposureReg</pValue>
    <Min>10</Min><Max>1000000</Max>
  </Float>
  <FloatReg Name="ExposureReg">
    <Address>0x0</Address><Length>8</Length><AccessMode>RW</AccessMode>
    <pPort>Device</pPort><Endianess>LittleEndian</Endianess>
  </FloatReg>
  <Enumeration Name="ExposureAuto">
    <EnumEntry Name="Off"><Value>0</Value></EnumEntry>
    <EnumEntry Name="Once"><pIsAvailable>OnceAvailable</pIsAvailable><Value>1</Value></EnumEntry>
    <EnumEntry Name="Continuous"><Value>2</Value></EnumEntry>
    <pValue>AutoReg</pValue>
  </Enumeration>
  <IntReg Name="AutoReg">
    <Address>0x10</Address><Length>4</Length><AccessMode>RW</AccessMode>
    <pPort>Device</pPort><Sign>Unsigned</Sign><Endianess>LittleEndian</Endianess>
  </IntReg>
  <IntReg Name="OnceAvailable">
    <Address>0x18</Address><Length>4</Length><AccessMode>RO</AccessMode>
    <pPort>Device</pPort><Sign>Unsigned</Sign><Endianess>LittleEndian</Endianess>
  </IntReg>
  <IntSwissKnife Name="AutoOn">
    <pVariable Name="A">AutoReg</pVariable><Formula>A&lt;&gt;0</Formula>
  </IntSwissKnife>
  <Integer Name="Width"><Value>640</Value><Min>16</Min><Max>1280</Max><Inc>16</Inc></Integer>
  <Command Name="TimestampLatch"><pValue>LatchReg</pValue><CommandValue>1</CommandValue></Command>
  <IntReg Name="LatchReg">
    <Address>0x14</Address><Length>4</Length><AccessMode>RW</AccessMode>
    <pPort>Device</pPort><Sign>Unsigned</Sign><Endianess>LittleEndian</Endianess>
  </IntReg>
  <Port Name="Device"/>
</RegisterDescription>
"""


class URL:
    def __init__(self, url):
        self.url = url


class StandInPort:
    """A GenTL port: device registers, with the XML at 0x10000."""

    name = "Device"

    def __init__(self, *, zipped=False):
        self.memory = bytearray(0x50)
        self.memory[0:8] = struct.pack("<d", 1000.0)
        self.memory[0x40:0x46] = b"Probe\0"
        if zipped:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("probe.xml", XML)
            self.xml, name = archive.getvalue(), "probe.zip"
        else:
            self.xml, name = XML.encode() + b"\0" * 16, "probe.xml"
        self.url_info_list = [URL(f"local:///{name};10000;{len(self.xml):x}")]

    def read(self, address, size):
        if address >= 0x10000:
            start = address - 0x10000
            return size, self.xml[start : start + size]
        return size, bytes(self.memory[address : address + size])

    def write(self, address, value):
        self.memory[address : address + len(value)] = value


@pytest.fixture(scope="module")
def genicam():
    try:
        return gentl.load_genicam()
    except ImportError as exc:
        pytest.skip(str(exc))


@pytest.fixture(params=[False, True], ids=["xml", "zip"])
def port(request):
    return StandInPort(zipped=request.param)


@pytest.fixture
def features(genicam, port):
    keep = []
    nodes = gentl._node_map(genicam, port, keep)
    return gentl._Features(genicam, nodes)


def test_strings(features):
    assert features.get_string("DeviceModelName") == "Probe"
    assert not features.is_writable("DeviceModelName")


def test_floats_read_write_and_range(features, port):
    assert features.get_float("ExposureTime") == 1000.0
    assert features.float_range("ExposureTime") == (10.0, 1_000_000.0)
    features.set_float("ExposureTime", 2000.0)
    assert struct.unpack("<d", port.memory[0:8])[0] == 2000.0


def test_a_lock_makes_a_node_read_only(features):
    features.set_enum("ExposureAuto", "Continuous")
    assert features.is_readable("ExposureTime")
    assert not features.is_writable("ExposureTime")
    with pytest.raises(DeviceError):
        features.set_float("ExposureTime", 2000.0)
    features.set_enum("ExposureAuto", "Off")
    assert features.is_writable("ExposureTime")


def test_enum_entries_leave_out_unavailable_ones(features):
    # The driver assumes genapi's symbolics are only the available entries.
    assert features.enum_entries("ExposureAuto") == ("Off", "Continuous")


def test_an_unknown_enum_entry_raises(features):
    with pytest.raises(DeviceError):
        features.set_enum("ExposureAuto", "Sometimes")


def test_integers_have_an_increment(features):
    assert features.get_int("Width") == 640
    assert features.int_range("Width") == (16, 1280, 16)
    features.set_int("Width", 320)
    assert features.get_int("Width") == 320


def test_commands_execute(features, port):
    features.execute("TimestampLatch")
    assert port.memory[0x14] == 1


def test_a_missing_node(features):
    assert not features.has("NoSuchNode")
    assert not features.is_readable("NoSuchNode")
    with pytest.raises(DeviceError):
        features.get_float("NoSuchNode")
