"""A fake ``genicam`` package (``gentl`` and ``genapi``), over `FakeCamera`s.

`FakeGenicam(cameras)` stands in for the EMVA ``genicam`` package with a
GenTL producer that finds those cameras. Pass it to the GenTL driver in place
of the real package. Names follow genicam 1.6.0, introspected on a lab Mac
(``docs/hardware/usb3-vision.md``).

The fake is strict where GenTL punishes mistakes:

- A module can't close while one it created is still open (a producer while
  its system is, a device while its data stream is), and a buffer can't be
  revoked while it is queued.
- A device opens exclusively once; a camera held elsewhere raises
  ``ResourceInUseException``.
- Frames flow only after both the data stream and the camera have started
  (``AcquisitionStart``), and each needs a queued buffer.

The stream follows the same arrival rule as `fakes.aravis`: a wait with a
timeout lets one frame arrive, while a zero-timeout wait only takes frames
already there (in trigger mode, those the triggers asked for).
"""

import enum
import io
import os
import zipfile
from collections import deque
from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import Any

from fakes.genicam import (
    PFNC,
    FakeCamera,
    FakeCameraError,
    FakeFrame,
    frame_bytes,
    payload_size,
)

__all__ = ["PRODUCER", "FakeGenicam"]

PRODUCER = "/opt/ids/cti/ids_u3vgentl.cti"

# --- gentl exceptions -------------------------------------------------------------


class GenericException(Exception):
    """Stands in for ``genicam.gentl.GenericException``."""


class TimeoutException(GenericException):
    pass


class ResourceInUseException(GenericException):
    pass


class AccessDeniedException(GenericException):
    pass


class BusyException(GenericException):
    pass


class LoadLibraryException(GenericException):
    pass


class InvalidParameterException(GenericException):
    pass


class NotAvailableException(GenericException):
    pass


class IoException(GenericException):
    pass


# --- genapi exceptions ------------------------------------------------------------


class GenApiGenericException(Exception):
    """Stands in for ``genicam.genapi.GenericException``."""


class LogicalErrorException(GenApiGenericException):
    pass


class AccessException(GenApiGenericException):
    pass


class InvalidArgumentException(GenApiGenericException):
    pass


class OutOfRangeException(GenApiGenericException):
    pass


class GenApiTimeoutException(GenApiGenericException):
    pass


def _genapi_error(exc: FakeCameraError) -> GenApiGenericException:
    kinds: dict[str, type[GenApiGenericException]] = {
        "not_found": LogicalErrorException,
        "access": AccessException,
        "range": OutOfRangeException,
        "timeout": GenApiTimeoutException,
    }
    return kinds.get(exc.kind, LogicalErrorException)(str(exc))


def _genapi(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except FakeCameraError as exc:
        error = _genapi_error(exc)
    raise error


class EAccessMode(enum.IntEnum):
    NI = 0
    NA = 1
    WO = 2
    RO = 3
    RW = 4


# --- genapi nodes -----------------------------------------------------------------


class _Node:
    """A typed node (``IFloat``, ``IEnumeration``, ...), over a node source."""

    def __init__(self, source: Any, name: str) -> None:
        self._source = source
        self._name = name

    def get_access_mode(self) -> EAccessMode:
        return EAccessMode[self._source.access(self._name)]

    @property
    def value(self) -> Any:
        return _genapi(lambda: self._source.read(self._name))

    @value.setter
    def value(self, value: Any) -> None:
        try:
            self._source.write(self._name, value)
        except FakeCameraError as exc:
            # genapi refuses an unknown enum entry as an invalid argument.
            error = (
                InvalidArgumentException(str(exc))
                if exc.kind == "range" and isinstance(value, str)
                else _genapi_error(exc)
            )
        else:
            return
        raise error


class IFloat(_Node):
    @property
    def min(self) -> float:
        return float(self._source.range(self._name)[0])

    @property
    def max(self) -> float:
        return float(self._source.range(self._name)[1])


class IInteger(_Node):
    @property
    def min(self) -> int:
        return int(self._source.range(self._name)[0])

    @property
    def max(self) -> int:
        return int(self._source.range(self._name)[1])

    @property
    def inc(self) -> int:
        return int(self._source.range(self._name)[2])


class IString(_Node):
    pass


class IEnumeration(_Node):
    @property
    def symbolics(self) -> tuple[str, ...]:
        return tuple(self._source.entries(self._name))


class ICommand(_Node):
    def execute(self) -> None:
        _genapi(lambda: self._source.execute(self._name))


_NODE_TYPES = {
    "float": IFloat,
    "int": IInteger,
    "string": IString,
    "enum": IEnumeration,
    "command": ICommand,
}


class _StreamNodes:
    """A data stream's node map: just ``StreamBufferHandlingMode``."""

    ENTRIES = ("OldestFirst", "OldestFirstOverwrite", "NewestOnly", "NewestFirst")

    def __init__(self) -> None:
        self.mode = "OldestFirst"

    def has(self, name: str) -> bool:
        return name == "StreamBufferHandlingMode"

    def kind(self, name: str) -> str:
        return "enum"

    def access(self, name: str) -> str:
        return "RW" if self.has(name) else "NA"

    def read(self, name: str) -> str:
        return self.mode

    def write(self, name: str, value: str) -> None:
        if value not in self.ENTRIES:
            raise FakeCameraError("range", f"no entry {value!r}")
        self.mode = value

    def entries(self, name: str) -> tuple[str, ...]:
        return self.ENTRIES


class NodeMap:
    """``genapi.NodeMap``: loads a fake XML that names its node source."""

    def __init__(self) -> None:
        self._source: Any = None
        self.connected_port: Any = None

    def load_xml_from_string(self, xml: str) -> None:
        source = _XML_SOURCES.get(xml.strip())
        if source is None:
            raise LogicalErrorException(f"unparseable XML: {xml[:40]!r}")
        self._source = source

    def connect(self, port: Any, name: str) -> None:
        self.connected_port = (port, name)

    def has_node(self, name: str) -> bool:
        return self._source.has(name)

    def get_node(self, name: str) -> _Node:
        if not self._source.has(name):
            raise LogicalErrorException("Node not existing")
        return _NODE_TYPES[self._source.kind(name)](self._source, name)


class AbstractPort:
    """``genapi.AbstractPort``, for the driver's port adapter to subclass."""


# XML text -> node source; filled as fake ports hand out their XML.
_XML_SOURCES: dict[str, Any] = {}

# --- gentl modules ----------------------------------------------------------------


class _Module:
    """Something that opens and closes, and may not close before its children."""

    def __init__(self, genicam: "FakeGenicam", label: str) -> None:
        self._genicam = genicam
        self._label = label
        self._children: list[_Module] = []
        self.is_open_ = False

    def _open(self, parent: "_Module | None") -> None:
        if self.is_open_:
            raise ResourceInUseException(f"{self._label} is already open")
        self.is_open_ = True
        self._genicam.open_modules.append(self)
        if parent is not None:
            parent._children.append(self)

    def close(self) -> None:
        if any(child.is_open_ for child in self._children):
            raise ResourceInUseException(f"{self._label} still has open children")
        if self.is_open_:
            self.is_open_ = False
            self._genicam.open_modules.remove(self)

    def is_open(self) -> bool:
        return self.is_open_


class URLInfo:
    def __init__(self, url: str) -> None:
        self.url = url


class Port:
    """A ``gentl.Port`` that serves a fake XML (padded, or zipped)."""

    def __init__(self, name: str, xml: str | None, *, zipped: bool = False) -> None:
        self.name = name
        if xml is None:
            self._data = b""
            self.url_info_list: list[URLInfo] = []
            return
        if zipped:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("camera.xml", xml)
            data, file_name = archive.getvalue(), "camera.zip"
        else:
            # Real ports pad the XML with NULs to the register size.
            data, file_name = xml.encode() + b"\0" * 7, "camera.xml"
        self._data = data
        self.url_info_list = [URLInfo(f"local:///{file_name};10000;{len(data):x}")]

    def read(self, address: int, size: int) -> tuple[int, bytes]:
        start = address - 0x10000
        return size, self._data[start : start + size]


class FakeGenTLBuffer:
    """A ``gentl.Buffer``: memory the driver announced, filled with a frame."""

    def __init__(self, token: "BufferToken") -> None:
        # The producer writes into the token's memory; the fake keeps its own.
        self.raw_buffer = bytearray(len(token.raw_buffer))
        self.context = token.context
        self.frame: FakeFrame | None = None
        self._complete = False

    def _filled(self) -> FakeFrame:
        if self.frame is None:
            raise NotAvailableException("the buffer holds no frame")
        return self.frame

    def is_complete(self) -> bool:
        self._filled()
        return self._complete

    @property
    def frame_id(self) -> int:
        return self._filled().frame_id

    @property
    def timestamp_ns(self) -> int:
        return self._filled().timestamp_ns

    @property
    def width(self) -> int:
        return int(self._filled().pixels.shape[1])

    @property
    def height(self) -> int:
        return int(self._filled().pixels.shape[0])

    @property
    def pixel_format(self) -> int:
        return PFNC[self._filled().pixel_format]

    @property
    def size_filled(self) -> int:
        frame = self._filled()
        return len(frame_bytes(frame.pixels, frame.pixel_format))

    def _fill(self, frame: FakeFrame) -> None:
        data = frame_bytes(frame.pixels, frame.pixel_format)
        self.frame = frame
        self._complete = frame.status.name == "COMPLETE" and len(data) <= len(
            self.raw_buffer
        )
        size = min(len(data), len(self.raw_buffer))
        self.raw_buffer[:size] = data[:size]

    def _empty(self) -> None:
        if self.frame is not None:
            self.frame.release()
            self.frame = None


class BufferToken:
    def __init__(self, raw_buffer: bytearray, context: Any) -> None:
        self.raw_buffer = raw_buffer
        self.context = context


class FakeDataStream(_Module):
    """A ``gentl.DataStream`` with its input and output queues."""

    def __init__(self, device: "FakeDevice") -> None:
        super().__init__(device._genicam, f"stream of {device.sim.serial}")
        self.device = device
        self.announced: list[FakeGenTLBuffer] = []
        self.input: deque[FakeGenTLBuffer] = deque()
        self.output: deque[FakeGenTLBuffer] = deque()
        self.started = False
        self.waits: list[int] = []
        nodes = _StreamNodes() if device._genicam.stream_nodes else None
        self.nodes = nodes
        xml = None
        if nodes is not None:
            xml = f"<stream of='{device.sim.serial}' id='{id(nodes)}'/>"
            _XML_SOURCES[xml] = nodes
        self.port = Port("DataStream", xml)

    @property
    def sim(self) -> FakeCamera:
        return self.device.sim

    def open(self, stream_id: str) -> None:
        self._open(self.device)

    def defines_payload_size(self) -> bool:
        return True

    @property
    def payload_size(self) -> int:
        return payload_size(self.sim)

    @property
    def buffer_announce_min(self) -> int:
        return 1

    def announce_buffer(self, token: BufferToken) -> FakeGenTLBuffer:
        # Verified against FLIR's producer: a bytearray is refused, bytes work.
        if not isinstance(token.raw_buffer, bytes):
            raise InvalidParameterException(
                "Parameter not valid or out of range. (Message from the source: "
                "Error calling GenTL function DSAnnounceBuffer.)"
            )
        buffer = FakeGenTLBuffer(token)
        self.announced.append(buffer)
        return buffer

    def queue_buffer(self, buffer: FakeGenTLBuffer) -> None:
        if buffer not in self.announced:
            raise InvalidParameterException("buffer wasn't announced")
        buffer._empty()
        self.input.append(buffer)

    def revoke_buffer(self, buffer: FakeGenTLBuffer) -> None:
        if buffer in self.input or buffer in self.output:
            raise ResourceInUseException("buffer is still queued")
        buffer._empty()
        self.announced.remove(buffer)

    def flush_buffer_queue(self, mode: int) -> None:
        for queue in (self.input, self.output):
            while queue:
                queue.popleft()._empty()

    def start_acquisition(self, flags: int, count: int) -> None:
        if self.started:
            raise ResourceInUseException("stream already started")
        self.started = True

    def stop_acquisition(self, flags: int) -> None:
        if not self.started:
            raise GenericException("stream isn't started")
        self.started = False

    def register_event(self, event_type: int) -> "FakeDataStream":
        return self

    def arrive(self, count: int = 1) -> int:
        """Let up to ``count`` frames land in the output queue; returns how many."""
        landed = 0
        while (
            landed < count
            and self.started
            and self.input
            and self.sim.streaming
            and self.sim.frame_waiting()
        ):
            buffer = self.input.popleft()
            buffer._fill(self.sim.next_frame())
            self.output.append(buffer)
            landed += 1
        return landed

    def close(self) -> None:
        if self.announced:
            raise ResourceInUseException("stream still has announced buffers")
        super().close()


class EventManagerNewBuffer:
    """``gentl.EventManagerNewBuffer``, over a fake stream's output queue."""

    def __init__(self, stream: FakeDataStream) -> None:
        self._stream = stream
        self.buffer: FakeGenTLBuffer | None = None

    def update_event_data(self, timeout_ms: int) -> None:
        stream = self._stream
        stream.waits.append(timeout_ms)
        if not stream.output:
            if timeout_ms > 0:
                stream.arrive()
            elif stream.sim.triggered:
                stream.arrive(len(stream.sim.pending_triggers) + len(stream.sim.queue))
        if not stream.output:
            raise TimeoutException(f"no buffer in {timeout_ms} ms")
        self.buffer = stream.output.popleft()

    def flush_event_queue(self) -> None:
        while self._stream.output:
            self._stream.input.append(self._stream.output.popleft())


class FakeDevice(_Module):
    """A ``gentl.Device``: the camera, opened through an interface."""

    def __init__(self, interface: "FakeInterface", sim: FakeCamera) -> None:
        super().__init__(interface._genicam, f"device {sim.serial}")
        self.interface = interface
        self.sim = sim
        self.data_stream_ids = ["Stream0"]
        xml = f"<camera serial='{sim.serial}' id='{id(sim)}'/>"
        _XML_SOURCES[xml] = sim
        self.remote_port = Port("Device", xml, zipped=self._genicam.zipped_xml)
        self.streams: list[FakeDataStream] = []

    def open(self, flags: int) -> None:
        if self.sim.opened and self._genicam.reopenable:
            # FLIR's producer lets the same process open a camera again (verified).
            self._open(self.interface)
            return
        try:
            self.sim.open()
        except FakeCameraError as exc:
            error = ResourceInUseException(str(exc))
        else:
            self._open(self.interface)
            return
        raise error

    def close(self) -> None:
        super().close()
        if self.sim.streaming:
            raise ResourceInUseException(f"camera {self.sim.serial} is still acquiring")
        self.sim.close()

    def create_data_stream(self) -> FakeDataStream:
        stream = FakeDataStream(self)
        self.streams.append(stream)
        return stream

    @property
    def timestamp_frequency(self) -> int:
        return 1_000_000_000


class FakeDeviceInfo:
    def __init__(self, interface: "FakeInterface", sim: FakeCamera) -> None:
        self._interface = interface
        self._sim = sim
        self.serial_number = sim.serial
        self.model = sim.model
        self.vendor = sim.vendor
        self.id_ = f"{sim.vendor.split()[0]}-{sim.serial}"

    def create_device(self) -> FakeDevice:
        return FakeDevice(self._interface, self._sim)


class FakeInterface(_Module):
    def __init__(self, system: "FakeSystem") -> None:
        super().__init__(system._genicam, "U3V interface")
        self._system = system
        self.device_info_list: list[FakeDeviceInfo] = []

    def open(self) -> None:
        self._open(self._system)

    def update_device_info_list(self, timeout_ms: int) -> None:
        self._genicam.device_updates += 1
        self.device_info_list = [
            FakeDeviceInfo(self, c) for c in self._genicam.cameras if c.attached
        ]


class FakeInterfaceInfo:
    def __init__(self, system: "FakeSystem") -> None:
        self._system = system
        self.id_ = "U3V"

    def create_interface(self) -> FakeInterface:
        return FakeInterface(self._system)


class FakeSystem(_Module):
    def __init__(self, producer: "FakeProducer") -> None:
        super().__init__(producer._genicam, "system")
        self._producer = producer
        self.interface_info_list: list[FakeInterfaceInfo] = []

    def open(self) -> None:
        self._open(self._producer)

    def update_interface_info_list(self, timeout_ms: int) -> None:
        if self._producer.path_name in self._genicam.listing_fails:
            raise GenericException("Unspecified runtime exception")
        self.interface_info_list = [FakeInterfaceInfo(self)]


class FakeProducer(_Module):
    def __init__(self, genicam: "FakeGenicam") -> None:
        super().__init__(genicam, "producer")
        self.path_name: str | None = None

    def open(self, path: str) -> None:
        if path not in self._genicam.producers:
            raise LoadLibraryException(f"can't load {path}")
        self.path_name = path
        if self._genicam.chatter:
            # Real producers' libraries write straight to the stderr descriptor.
            os.write(2, self._genicam.chatter)
        self._genicam.producers_opened.append(path)
        self._open(None)

    def create_system(self) -> FakeSystem:
        return FakeSystem(self)


class FakeGenicam:
    """Stands in for the ``genicam`` package, with ``gentl`` and ``genapi``.

    Args:
        cameras: The cameras attached.
        producers: Paths of the ``.cti`` files that load.
        zipped_xml: Whether cameras serve their XML zipped.
        stream_nodes: Whether data streams have a ``StreamBufferHandlingMode``.
        reopenable: Whether a camera this process has open can be opened again,
            as FLIR's producer allows.
        chatter: Bytes the producer writes to file descriptor 2 as it loads, as
            genicam does for functions an older producer lacks.
        listing_fails: Paths of producers that load but fail to list interfaces.
    """

    def __init__(
        self,
        cameras: Iterable[FakeCamera] = (),
        *,
        producers: Iterable[str] = (PRODUCER,),
        zipped_xml: bool = False,
        stream_nodes: bool = True,
        reopenable: bool = False,
        chatter: bytes = b"",
        listing_fails: Iterable[str] = (),
    ) -> None:
        self.listing_fails = set(listing_fails)
        self.reopenable = reopenable
        self.chatter = chatter
        self.cameras = list(cameras)
        self.producers = list(producers)
        self.zipped_xml = zipped_xml
        self.stream_nodes = stream_nodes
        self.open_modules: list[_Module] = []
        self.producers_opened: list[str] = []
        self.device_updates = 0
        genicam = self

        class GenTLProducer(FakeProducer):
            @staticmethod
            def create_producer() -> FakeProducer:
                return FakeProducer(genicam)

        flags = SimpleNamespace
        self.gentl = SimpleNamespace(
            GenTLProducer=GenTLProducer,
            BufferToken=BufferToken,
            EventManagerNewBuffer=EventManagerNewBuffer,
            GenericException=GenericException,
            TimeoutException=TimeoutException,
            ResourceInUseException=ResourceInUseException,
            AccessDeniedException=AccessDeniedException,
            BusyException=BusyException,
            LoadLibraryException=LoadLibraryException,
            NotAvailableException=NotAvailableException,
            DEVICE_ACCESS_FLAGS_LIST=flags(DEVICE_ACCESS_EXCLUSIVE=3),
            ACQ_START_FLAGS_LIST=flags(ACQ_START_FLAGS_DEFAULT=0),
            ACQ_STOP_FLAGS_LIST=flags(ACQ_STOP_FLAGS_DEFAULT=0, ACQ_STOP_FLAGS_KILL=1),
            ACQ_QUEUE_TYPE_LIST=flags(ACQ_QUEUE_ALL_DISCARD=4),
            EVENT_TYPE_LIST=flags(EVENT_NEW_BUFFER=1),
        )
        self.genapi = SimpleNamespace(
            NodeMap=NodeMap,
            AbstractPort=AbstractPort,
            EAccessMode=EAccessMode,
            GenericException=GenApiGenericException,
            TimeoutException=GenApiTimeoutException,
            LogicalErrorException=LogicalErrorException,
            AccessException=AccessException,
        )

    def streams(self) -> list[FakeDataStream]:
        """Every data stream open now."""
        return [m for m in self.open_modules if isinstance(m, FakeDataStream)]
