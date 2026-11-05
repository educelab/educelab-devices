"""A fake ``PySpin`` module over `fakes.genicam.FakeCamera`s.

`FakePySpin(cameras)` builds a fresh module object with its own ``System``, so
tests stay independent. The cameras are the shared simulation; this module is
only PySpin's shape on top of it: node pointers, the ``System`` and its camera
lists, error codes, and image statuses. `flir_camera()` makes a simulated
camera with PySpin-flavoured defaults.

The fake is strict where PySpin punishes mistakes, so driver bugs fail here
rather than on a rig:

- ``System.ReleaseInstance()`` fails while a camera list is uncleared, a camera
  pointer is still referenced, or a camera is still initialized.
- Iterating a ``CameraList`` stops early if the list is cleared mid-loop (#90).
- Camera nodes read as ``NA`` until ``Init()``; transport-layer nodes don't.
- Integer nodes take only ints.

Behavior that hasn't been checked against real PySpin is marked "assumed" in
``docs/hardware/spinnaker.md``.
"""

import weakref
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import numpy as np

from educelab.devices.cameras._genicam import BufferStatus
from fakes.genicam import (
    ENUMS,
    LAYOUTS,
    FakeCamera,
    FakeCameraError,
    FakeFrame,
    FrameSpec,
)

__all__ = [
    "CameraList",
    "CameraPtr",
    "FakeImage",
    "FakePySpin",
    "FakeSystem",
    "FrameSpec",
    "LibraryVersion",
    "SpinnakerException",
    "flir_camera",
]

# Names and values below match PySpin 4.4.0 (tests/test_fake_pyspin.py checks
# them against the real module when it is installed).

# GenApi access modes.
NI, NA, WO, RO, RW = range(5)
_ACCESS = {"NA": NA, "WO": WO, "RO": RO, "RW": RW}

SPINNAKER_ERR_SUCCESS = 0
SPINNAKER_ERR_ERROR = -1001
SPINNAKER_ERR_NOT_INITIALIZED = -1002
SPINNAKER_ERR_RESOURCE_IN_USE = -1004
SPINNAKER_ERR_ACCESS_DENIED = -1005
SPINNAKER_ERR_INVALID_HANDLE = -1006
SPINNAKER_ERR_INVALID_PARAMETER = -1009
SPINNAKER_ERR_IO = -1010
SPINNAKER_ERR_TIMEOUT = -1011

EVENT_TIMEOUT_INFINITE = 0xFFFFFFFFFFFFFFFF
EVENT_TIMEOUT_NONE = 0

# The pixel formats PySpin names; others get values past these.
_PYSPIN_FORMATS = ("Mono8", "Mono16", "RGB8", "BayerRG8", "BayerRG16", "Mono12Packed")
_STREAM_BUFFER_MODES = (
    "OldestFirst",
    "OldestFirstOverwrite",
    "NewestOnly",
    "NewestFirst",
)
_EXPORTED_ENUMS = {
    **ENUMS,
    "PixelFormat": _PYSPIN_FORMATS,
    "StreamBufferHandlingMode": _STREAM_BUFFER_MODES,
}
_ENUMS = {
    **_EXPORTED_ENUMS,
    "PixelFormat": (
        *_PYSPIN_FORMATS,
        *(f for f in LAYOUTS if f not in _PYSPIN_FORMATS),
    ),
}

# Image statuses, with the descriptions their names suggest.
_IMAGE_STATUSES = {
    "SPINNAKER_IMAGE_STATUS_UNKNOWN_ERROR": -1,
    "SPINNAKER_IMAGE_STATUS_NO_ERROR": 0,
    "SPINNAKER_IMAGE_STATUS_CRC_CHECK_FAILED": 1,
    "SPINNAKER_IMAGE_STATUS_DATA_OVERFLOW": 2,
    "SPINNAKER_IMAGE_STATUS_MISSING_PACKETS": 3,
    "SPINNAKER_IMAGE_STATUS_LEADER_BUFFER_SIZE_INCONSISTENT": 4,
    "SPINNAKER_IMAGE_STATUS_TRAILER_BUFFER_SIZE_INCONSISTENT": 5,
    "SPINNAKER_IMAGE_STATUS_PACKETID_INCONSISTENT": 6,
    "SPINNAKER_IMAGE_STATUS_MISSING_LEADER": 7,
    "SPINNAKER_IMAGE_STATUS_MISSING_TRAILER": 8,
    "SPINNAKER_IMAGE_STATUS_DATA_INCOMPLETE": 9,
    "SPINNAKER_IMAGE_STATUS_INFO_INCONSISTENT": 10,
    "SPINNAKER_IMAGE_STATUS_CHUNK_DATA_INVALID": 11,
    "SPINNAKER_IMAGE_STATUS_NO_SYSTEM_RESOURCES": 12,
    "SPINNAKER_IMAGE_STATUS_GENDC_DATA_INVALID": 13,
    "SPINNAKER_IMAGE_STATUS_GENDC_PART_DATA_INVALID": 14,
}
SPINNAKER_IMAGE_STATUS_NO_ERROR = 0
SPINNAKER_IMAGE_STATUS_MISSING_PACKETS = 3
SPINNAKER_IMAGE_STATUS_MISSING_TRAILER = 8
SPINNAKER_IMAGE_STATUS_DATA_INCOMPLETE = 9

CONSTANTS: dict[str, int] = {
    "NI": NI,
    "NA": NA,
    "WO": WO,
    "RO": RO,
    "RW": RW,
    "EVENT_TIMEOUT_INFINITE": EVENT_TIMEOUT_INFINITE,
    "EVENT_TIMEOUT_NONE": EVENT_TIMEOUT_NONE,
    **{
        name: value
        for name, value in globals().items()
        if name.startswith("SPINNAKER_ERR_")
    },
    **_IMAGE_STATUSES,
    **{
        f"{family}_{entry}": i
        for family, entries in _EXPORTED_ENUMS.items()
        for i, entry in enumerate(entries)
    },
}

# How each FakeCamera failure surfaces through PySpin (assumed; hardware doc).
_CODES = {
    "not_found": SPINNAKER_ERR_ERROR,
    "access": SPINNAKER_ERR_ACCESS_DENIED,
    "range": SPINNAKER_ERR_INVALID_PARAMETER,
    "timeout": SPINNAKER_ERR_TIMEOUT,
    "state": SPINNAKER_ERR_ERROR,
    "busy": SPINNAKER_ERR_ACCESS_DENIED,
}


def flir_camera(
    serial: str = "20000001",
    model: str = "Blackfly S BFS-U3-51S5M",
    *,
    color: bool = False,
    **options: Any,
) -> FakeCamera:
    """A simulated camera as a FLIR one comes: vendor, firmware and formats."""
    formats = ["Mono8", "Mono16"]
    if color:
        formats = ["RGB8", "BayerRG8", "BayerRG16", *formats]
    options.setdefault("formats", formats)
    options.setdefault("firmware", "1.0.0.0")
    return FakeCamera(serial, model, vendor="FLIR", color=color, **options)


class SpinnakerException(Exception):
    """Stands in for ``PySpin.SpinnakerException``."""

    def __init__(self, message: str, errorcode: int = SPINNAKER_ERR_ERROR) -> None:
        super().__init__(f"Spinnaker: {message} [{errorcode}]")
        self.message = message
        self.errorcode = errorcode
        self.fullmessage = str(self)


def _call(sim: FakeCamera, fn: Callable[[], Any], *, state: int | None = None) -> Any:
    """Run ``fn`` on the simulation, raising its failure as PySpin would.

    Args:
        state: The code for a ``state`` failure on an initialized camera.
    """
    if not sim.attached:
        raise SpinnakerException(f"camera {sim.serial} lost", SPINNAKER_ERR_IO)
    try:
        return fn()
    except FakeCameraError as exc:
        code = _CODES[exc.kind]
        if exc.kind == "state":
            code = state or code if sim.opened else SPINNAKER_ERR_NOT_INITIALIZED
        error = SpinnakerException(str(exc), code)
    # Raised outside the except block and dropped from this frame, so the
    # traceback holds no cycle that would keep a camera pointer alive.
    try:
        raise error
    finally:
        del error


def _enum_value(family: str, symbol: str) -> int:
    return _ENUMS[family].index(symbol)


def _enum_symbol(family: str, value: int) -> str | None:
    entries = _ENUMS[family]
    return entries[value] if 0 <= value < len(entries) else None


# --- Node maps --------------------------------------------------------------


class _CameraNodes:
    """The camera's own node map, read through to the simulation."""

    def __init__(self, sim: FakeCamera) -> None:
        self.sim = sim

    def has(self, name: str) -> bool:
        return self.sim.has(name)

    def kind(self, name: str) -> str:
        return self.sim.kind(name)

    def access(self, name: str) -> int:
        return _ACCESS[self.sim.access(name)]

    def read(self, name: str) -> Any:
        return _call(self.sim, lambda: self.sim.read(name))

    def write(self, name: str, value: Any) -> None:
        _call(self.sim, lambda: self.sim.write(name, value))

    def range(self, name: str) -> tuple[Any, ...]:
        self._require_readable(name)
        return tuple(self.sim.range(name))

    def entries(self, name: str) -> tuple[str, ...]:
        return self.sim.entries(name)

    def execute(self, name: str) -> None:
        _call(self.sim, lambda: self.sim.execute(name))

    def _require_readable(self, name: str) -> None:
        _call(self.sim, lambda: None)
        if self.access(name) not in (RO, RW):
            raise SpinnakerException(
                f"{name} is not readable", SPINNAKER_ERR_ACCESS_DENIED
            )


@dataclass
class _LocalNode:
    kind: str
    value: Any
    access: int = RO
    entries: tuple[str, ...] = ()


class _TransportNodes:
    """A transport-layer node map, which PySpin keeps itself.

    Its nodes are readable before ``Init()``. Tests change a node's ``access``
    or put an exception in ``failures`` to make reads of it fail.
    """

    def __init__(self, sim: FakeCamera, nodes: dict[str, _LocalNode]) -> None:
        self.sim = sim
        self.nodes = nodes
        self.failures: dict[str, SpinnakerException] = {}

    def has(self, name: str) -> bool:
        return name in self.nodes

    def kind(self, name: str) -> str:
        return self.nodes[name].kind

    def access(self, name: str) -> int:
        return self.nodes[name].access

    def read(self, name: str) -> Any:
        self._check(name, (RO, RW), "readable")
        return self.nodes[name].value

    def write(self, name: str, value: Any) -> None:
        self._check(name, (WO, RW), "writable")
        node = self.nodes[name]
        if node.kind == "enum" and value not in node.entries:
            raise SpinnakerException(
                f"{name}: no entry {value!r}", SPINNAKER_ERR_INVALID_PARAMETER
            )
        node.value = value

    def entries(self, name: str) -> tuple[str, ...]:
        return self.nodes[name].entries

    def _check(self, name: str, modes: tuple[int, ...], verb: str) -> None:
        _call(self.sim, lambda: None)
        if name in self.failures:
            failure = self.failures[name]
            # A fresh exception each time: raising the stored one would keep
            # its traceback, and the camera pointer in it, alive.
            raise SpinnakerException(failure.message, failure.errorcode)
        if self.access(name) not in modes:
            raise SpinnakerException(
                f"{name} is not {verb}", SPINNAKER_ERR_ACCESS_DENIED
            )


_NodeSource = _CameraNodes | _TransportNodes


# --- Nodes ------------------------------------------------------------------


class Node:
    """A GenICam node, read through its node map."""

    def __init__(self, source: _NodeSource, name: str) -> None:
        self._source = source
        self._name = name

    def GetName(self) -> str:
        return self._name

    def GetAccessMode(self) -> int:
        return self._source.access(self._name)


class ValueNode(Node):
    def GetValue(self) -> Any:
        return self._source.read(self._name)

    def SetValue(self, value: Any) -> None:
        self._source.write(self._name, value)

    def ToString(self) -> str:
        return str(self.GetValue())


class StringNode(ValueNode):
    pass


class FloatNode(ValueNode):
    def GetMin(self) -> float:
        return self._source.range(self._name)[0]  # type: ignore[union-attr]

    def GetMax(self) -> float:
        return self._source.range(self._name)[1]  # type: ignore[union-attr]


class IntegerNode(FloatNode):
    """An integer node. Like PySpin, it takes only ints."""

    def GetInc(self) -> int:
        return self._source.range(self._name)[2]  # type: ignore[union-attr]

    def SetValue(self, value: Any) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise SpinnakerException(
                f"{self._name} takes an int, not {type(value).__name__}",
                SPINNAKER_ERR_INVALID_PARAMETER,
            )
        super().SetValue(value)


@dataclass(frozen=True)
class EnumEntry:
    family: str
    symbolic: str
    value: int

    def GetSymbolic(self) -> str:
        return self.symbolic

    def GetValue(self) -> int:
        return self.value

    def GetName(self) -> str:
        return f"EnumEntry_{self.family}_{self.symbolic}"

    def GetAccessMode(self) -> int:
        return RO


class EnumNode(ValueNode):
    """An enumeration, valued by the ``PySpin.<Family>_<Entry>`` constants."""

    def _entry(self, symbol: str) -> EnumEntry:
        return EnumEntry(self._name, symbol, _enum_value(self._name, symbol))

    def GetValue(self) -> int:
        return _enum_value(self._name, self._source.read(self._name))

    def SetValue(self, value: Any) -> None:
        symbol = _enum_symbol(self._name, value)
        if symbol not in self._source.entries(self._name):
            raise SpinnakerException(
                f"{self._name}: no entry with value {value}",
                SPINNAKER_ERR_INVALID_PARAMETER,
            )
        self._source.write(self._name, symbol)

    def GetIntValue(self) -> int:
        return self.GetValue()

    def SetIntValue(self, value: int) -> None:
        self.SetValue(value)

    def GetCurrentEntry(self) -> EnumEntry:
        return self._entry(self._source.read(self._name))

    def GetEntries(self) -> list[EnumEntry]:
        return [self._entry(s) for s in self._source.entries(self._name)]

    def GetEntryByName(self, symbolic: str) -> EnumEntry | None:
        if symbolic not in self._source.entries(self._name):
            return None
        return self._entry(symbolic)

    def ToString(self) -> str:
        return self.GetCurrentEntry().symbolic


class CommandNode(Node):
    def Execute(self) -> None:
        self._source.execute(self._name)  # type: ignore[union-attr]

    def IsDone(self) -> bool:
        return True


_NODE_CLASSES: dict[str, type[Node]] = {
    "float": FloatNode,
    "int": IntegerNode,
    "string": StringNode,
    "enum": EnumNode,
    "command": CommandNode,
}


class NodeMap:
    def __init__(self, source: _NodeSource, names: Callable[[], Iterable[str]]):
        self._source = source
        self._names = names

    def GetNode(self, name: str) -> Node | None:
        if not self._source.has(name):
            return None
        return _NODE_CLASSES[self._source.kind(name)](self._source, name)

    def GetNodes(self) -> list[Node]:
        return [node for name in self._names() if (node := self.GetNode(name))]


class _NodeAttributes:
    """QuickSpin-style access, e.g. ``cam.TLDevice.DeviceSerialNumber``."""

    def __init__(self, nodes: NodeMap) -> None:
        self._nodes = nodes

    def __getattr__(self, name: str) -> Any:
        node = self._nodes.GetNode(name)
        if node is None:
            raise AttributeError(name)
        return node


def IsAvailable(node: Any) -> bool:
    return node is not None and node.GetAccessMode() not in (NI, NA)


def IsImplemented(node: Any) -> bool:
    return node is not None and node.GetAccessMode() != NI


def IsReadable(node: Any) -> bool:
    return node is not None and node.GetAccessMode() in (RO, RW)


def IsWritable(node: Any) -> bool:
    return node is not None and node.GetAccessMode() in (WO, RW)


def _cast(node: Node | None) -> Node | None:
    return node


class EnumerationPtr:
    """What ``PySpin.CEnumerationPtr(node)`` gives: an enumeration without the
    ``GetValue``/``SetValue`` that QuickSpin attributes (``cam.PixelFormat``) have.
    """

    def __init__(self, node: EnumNode) -> None:
        self._node = node

    def GetAccessMode(self) -> int:
        return self._node.GetAccessMode()

    def GetName(self) -> str:
        return self._node.GetName()

    def GetIntValue(self) -> int:
        return self._node.GetIntValue()

    def SetIntValue(self, value: int) -> None:
        self._node.SetIntValue(value)

    def GetCurrentEntry(self) -> EnumEntry:
        return self._node.GetCurrentEntry()

    def GetEntries(self) -> list[EnumEntry]:
        return self._node.GetEntries()

    def GetEntryByName(self, symbolic: str) -> EnumEntry | None:
        return self._node.GetEntryByName(symbolic)

    def ToString(self) -> str:
        return self._node.ToString()


def _enumeration(node: Node | None) -> EnumerationPtr | None:
    if node is None:
        return None
    if not isinstance(node, EnumNode):
        raise SpinnakerException(f"{node.GetName()} is not an enumeration")
    return EnumerationPtr(node)


# --- Images -----------------------------------------------------------------


class Image:
    """Stands in for ``PySpin.Image``, which holds the status descriptions."""

    @staticmethod
    def GetImageStatusDescription(status: int) -> str:
        name = next(n for n, v in _IMAGE_STATUSES.items() if v == status)
        return name.removeprefix("SPINNAKER_IMAGE_STATUS_").replace("_", " ").lower()


class FakeImage:
    """A grabbed buffer. It stays held until ``Release()``."""

    RELEASED_FILL = FakeFrame.RELEASED_FILL

    def __init__(self, frame: FakeFrame, status: int) -> None:
        self._frame = frame
        self._status = status

    def _held(self) -> FakeFrame:
        if self._frame.released:
            raise SpinnakerException("image was released", SPINNAKER_ERR_INVALID_HANDLE)
        return self._frame

    def GetNDArray(self) -> np.ndarray[Any, Any]:
        return self._held().pixels.view()

    def GetTimeStamp(self) -> int:
        return self._held().timestamp_ns

    def GetFrameID(self) -> int:
        return self._held().frame_id

    def GetImageStatus(self) -> int:
        self._held()
        return self._status

    def IsIncomplete(self) -> bool:
        self._held()
        return self._status != SPINNAKER_IMAGE_STATUS_NO_ERROR

    def GetPixelFormat(self) -> int:
        return _enum_value("PixelFormat", self._frame.pixel_format)

    def GetPixelFormatName(self) -> str:
        return self._frame.pixel_format

    def GetWidth(self) -> int:
        return self._frame.pixels.shape[1]

    def GetHeight(self) -> int:
        return self._frame.pixels.shape[0]

    def IsInUse(self) -> bool:
        return not self._frame.released

    def Release(self) -> None:
        self._held().release()


# --- Cameras ----------------------------------------------------------------


class _Transport:
    """What PySpin keeps for one camera: its transport-layer node maps."""

    def __init__(self, sim: FakeCamera) -> None:
        self.device = _TransportNodes(
            sim,
            {
                "DeviceSerialNumber": _LocalNode("string", sim.serial),
                "DeviceModelName": _LocalNode("string", sim.model),
                "DeviceVendorName": _LocalNode("string", sim.vendor),
            },
        )
        self.stream = _TransportNodes(
            sim,
            {
                "StreamBufferHandlingMode": _LocalNode(
                    "enum", "OldestFirst", RW, _STREAM_BUFFER_MODES
                )
            },
        )


class CameraPtr:
    """A reference to a camera, as handed out by a `CameraList`.

    The system counts live pointers, so a driver that keeps one (even as a
    leftover loop variable) cannot release the system.
    """

    def __init__(self, sim: FakeCamera | None, system: "FakeSystem") -> None:
        self._sim = sim
        self._system = system
        if sim is not None:
            self.TLDevice = _NodeAttributes(self.GetTLDeviceNodeMap())
            self.TLStream = _NodeAttributes(self.GetTLStreamNodeMap())
        system._live_pointers.add(self)

    def __getattr__(self, name: str) -> Any:
        sim = self.__dict__.get("_sim")
        if sim is None or not sim.has(name):
            raise AttributeError(name)
        return self.GetNodeMap().GetNode(name)

    def _cam(self) -> FakeCamera:
        if self._sim is None:
            raise SpinnakerException(
                "invalid camera pointer", SPINNAKER_ERR_INVALID_HANDLE
            )
        return self._sim

    def IsValid(self) -> bool:
        return self._sim is not None

    def GetUniqueID(self) -> str:
        return self._cam().serial

    def Init(self) -> None:
        # Initializing a camera this program already has does nothing.
        sim = self._cam()
        if sim.opened and sim.attached:
            return
        _call(sim, sim.open)

    def DeInit(self) -> None:
        sim = self._cam()
        _call(sim, sim.close, state=SPINNAKER_ERR_RESOURCE_IN_USE)

    def IsInitialized(self) -> bool:
        return self._cam().opened

    def IsStreaming(self) -> bool:
        return self._cam().streaming

    def BeginAcquisition(self) -> None:
        sim = self._cam()
        _call(sim, sim.start, state=SPINNAKER_ERR_RESOURCE_IN_USE)

    def EndAcquisition(self) -> None:
        sim = self._cam()
        _call(sim, sim.stop)

    def GetNextImage(self, grabTimeout: int = EVENT_TIMEOUT_INFINITE) -> FakeImage:
        # Real PySpin blocks until the grab timeout; the simulation fails at once.
        sim = self._cam()
        frame = _call(sim, sim.next_frame)
        return FakeImage(frame, self._system.module.image_status[frame.status])

    def GetNodeMap(self) -> NodeMap:
        sim = self._cam()
        return NodeMap(_CameraNodes(sim), lambda: list(sim.nodes))

    def GetTLDeviceNodeMap(self) -> NodeMap:
        nodes = self._system.module.transport(self._cam()).device
        return NodeMap(nodes, lambda: list(nodes.nodes))

    def GetTLStreamNodeMap(self) -> NodeMap:
        nodes = self._system.module.transport(self._cam()).stream
        return NodeMap(nodes, lambda: list(nodes.nodes))


class CameraList:
    """The cameras attached when ``GetCameras()`` was called."""

    def __init__(self, system: "FakeSystem", cameras: Iterable[FakeCamera]) -> None:
        self._system = system
        self._cameras = list(cameras)

    def GetSize(self) -> int:
        return len(self._cameras)

    def __len__(self) -> int:
        return self.GetSize()

    def GetByIndex(self, index: int) -> CameraPtr:
        if not 0 <= index < len(self._cameras):
            raise SpinnakerException(
                f"no camera at index {index}", SPINNAKER_ERR_INVALID_PARAMETER
            )
        return CameraPtr(self._cameras[index], self._system)

    __getitem__ = GetByIndex

    def GetBySerial(self, serial: str) -> CameraPtr:
        for camera in self._cameras:
            if camera.serial == serial:
                return CameraPtr(camera, self._system)
        return CameraPtr(None, self._system)

    def __iter__(self) -> Iterator[CameraPtr]:
        # Re-checks the size each step, so clearing mid-loop ends it (#90).
        index = 0
        while index < len(self._cameras):
            yield self.GetByIndex(index)
            index += 1

    def Clear(self) -> None:
        self._cameras.clear()
        self._system._uncleared_lists.discard(self)


@dataclass(frozen=True)
class LibraryVersion:
    major: int
    minor: int
    type: int
    build: int


class FakeSystem:
    """The ``PySpin.System`` singleton.

    ``GetInstance()`` and ``ReleaseInstance()`` are counted, so tests can check
    the driver takes and releases the system exactly once.
    """

    def __init__(self, module: "FakePySpin") -> None:
        self.module = module
        self.cameras: list[FakeCamera] = []
        self.version = LibraryVersion(4, 4, 0, 0)
        self.instances = 0
        self.get_instance_calls = 0
        self.release_instance_calls = 0
        self.get_instance_error: Exception | None = None
        self._uncleared_lists: set[CameraList] = set()
        self._live_pointers: weakref.WeakSet[CameraPtr] = weakref.WeakSet()

    @property
    def live_pointers(self) -> int:
        """Camera pointers still referenced somewhere."""
        return len(self._live_pointers)

    @property
    def uncleared_lists(self) -> int:
        return len(self._uncleared_lists)

    def GetInstance(self) -> "FakeSystem":
        self.get_instance_calls += 1
        if self.get_instance_error is not None:
            raise self.get_instance_error
        self.instances += 1
        return self

    def _require_instance(self) -> None:
        if self.instances < 1:
            raise SpinnakerException(
                "system was released", SPINNAKER_ERR_INVALID_HANDLE
            )

    def GetLibraryVersion(self) -> LibraryVersion:
        self._require_instance()
        return self.version

    def GetCameras(self) -> CameraList:
        self._require_instance()
        cameras = CameraList(self, self.cameras)
        self._uncleared_lists.add(cameras)
        return cameras

    def ReleaseInstance(self) -> None:
        self.release_instance_calls += 1
        self._require_instance()
        held = []
        if self._uncleared_lists:
            held.append(f"{self.uncleared_lists} camera list(s) not cleared")
        if self._live_pointers:
            held.append(f"{self.live_pointers} camera reference(s) still held")
        initialized = [c.serial for c in self.cameras if c.opened]
        if initialized:
            held.append(f"camera(s) {', '.join(initialized)} still initialized")
        if held:
            raise SpinnakerException(
                f"can't release the system: {'; '.join(held)}",
                SPINNAKER_ERR_RESOURCE_IN_USE,
            )
        self.instances -= 1


class FakePySpin(ModuleType):
    """A stand-in for the ``PySpin`` module with its own ``System``.

    Args:
        cameras: Cameras attached from the start.

    Attributes:
        image_status: The image status each `BufferStatus` is reported as.
            Change it to report a different reason for an incomplete frame.
    """

    SpinnakerException = SpinnakerException
    IsAvailable = staticmethod(IsAvailable)
    IsImplemented = staticmethod(IsImplemented)
    IsReadable = staticmethod(IsReadable)
    IsWritable = staticmethod(IsWritable)
    Image = Image
    CEnumerationPtr = staticmethod(_enumeration)
    CValuePtr = CFloatPtr = CIntegerPtr = staticmethod(_cast)
    CStringPtr = CBooleanPtr = CCommandPtr = CEnumEntryPtr = staticmethod(_cast)

    def __init__(self, cameras: Iterable[FakeCamera] = ()) -> None:
        super().__init__("PySpin")
        for name, value in CONSTANTS.items():
            setattr(self, name, value)
        self.image_status = {
            BufferStatus.COMPLETE: SPINNAKER_IMAGE_STATUS_NO_ERROR,
            BufferStatus.MISSING_TRAILER: SPINNAKER_IMAGE_STATUS_MISSING_TRAILER,
            BufferStatus.INCOMPLETE: SPINNAKER_IMAGE_STATUS_DATA_INCOMPLETE,
        }
        self._transports: dict[int, _Transport] = {}
        self.system = FakeSystem(self)
        self.System = self.system
        for camera in cameras:
            self.attach(camera)

    def transport(self, sim: FakeCamera) -> _Transport:
        """The transport-layer node maps PySpin keeps for a camera."""
        return self._transports.setdefault(id(sim), _Transport(sim))

    def attach(self, camera: FakeCamera) -> FakeCamera:
        """Plug a camera in."""
        camera.attached = True
        self.transport(camera)
        self.system.cameras.append(camera)
        return camera

    def detach(self, serial: str) -> FakeCamera:
        """Unplug a camera. Existing pointers to it start failing."""
        camera = next(c for c in self.system.cameras if c.serial == serial)
        camera.attached = False
        camera.opened = False
        camera.streaming = False
        self.system.cameras.remove(camera)
        return camera
