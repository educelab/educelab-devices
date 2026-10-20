"""A fake ``PySpin`` module, for testing the Spinnaker driver without the SDK.

`FakePySpin()` builds a fresh module object with its own ``System``, so tests
stay independent. Attach `FakeCamera`s to it and pass it to the driver in
place of the real module.

The fake is strict where PySpin punishes mistakes, so driver bugs fail here
rather than on a rig:

- ``System.ReleaseInstance()`` fails while a camera list is uncleared, a camera
  pointer is still referenced, or a camera is still initialized.
- Iterating a ``CameraList`` stops early if the list is cleared mid-loop (#90).
- In trigger mode, a fresh frame needs a ``TriggerSoftware``; without one,
  ``GetNextImage()`` times out. ``TriggerSource`` is writable only while
  ``TriggerMode`` is off.
- Every ``GetNextImage()`` buffer must be released. Once a camera's buffers are
  all held, ``GetNextImage()`` times out (#83). A released buffer's pixels are
  overwritten, so arrays used without a copy show garbage.

Behavior that hasn't been checked against real PySpin is marked "assumed" in
``docs/hardware/spinnaker.md``.
"""

import time
import weakref
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import numpy as np

__all__ = [
    "CameraList",
    "CameraPtr",
    "FakeCamera",
    "FakeImage",
    "FakePySpin",
    "FakeSystem",
    "FrameSpec",
    "SpinnakerException",
]

# Names and values below match PySpin 4.4.0 (tests/test_fake_pyspin.py checks
# them against the real module when it is installed).

# GenApi access modes.
NI, NA, WO, RO, RW = range(5)

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

_ENUMS = {
    "PixelFormat": (
        "Mono8",
        "Mono16",
        "RGB8",
        "BayerRG8",
        "BayerRG16",
        "Mono12Packed",
    ),
    "PixelColorFilter": ("None", "BayerRG", "BayerGB", "BayerGR", "BayerBG"),
    "ExposureAuto": ("Off", "Once", "Continuous"),
    "GainAuto": ("Off", "Once", "Continuous"),
    "ExposureMode": ("Timed", "TriggerWidth"),
    "AcquisitionMode": ("Continuous", "SingleFrame", "MultiFrame"),
    "TriggerMode": ("Off", "On"),
    "DeviceLinkThroughputLimitMode": ("Off", "On"),
    "TriggerSelector": ("FrameStart", "AcquisitionStart", "FrameBurstStart"),
    "TriggerSource": ("Software", "Line0", "Line1", "Line2", "Line3"),
    "StreamBufferHandlingMode": (
        "OldestFirst",
        "OldestFirstOverwrite",
        "NewestOnly",
        "NewestFirst",
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
        for family, entries in _ENUMS.items()
        for i, entry in enumerate(entries)
    },
}

# (dtype, channels) of each pixel format's array.
_LAYOUTS: dict[str, tuple[type[np.unsignedinteger[Any]], int]] = {
    "Mono8": (np.uint8, 1),
    "Mono16": (np.uint16, 1),
    "RGB8": (np.uint8, 3),
    "BayerRG8": (np.uint8, 1),
    "BayerRG16": (np.uint16, 1),
    "Mono12Packed": (np.uint8, 1),
}


class SpinnakerException(Exception):
    """Stands in for ``PySpin.SpinnakerException``."""

    def __init__(self, message: str, errorcode: int = SPINNAKER_ERR_ERROR) -> None:
        super().__init__(f"Spinnaker: {message} [{errorcode}]")
        self.message = message
        self.errorcode = errorcode
        self.fullmessage = str(self)


# --- Nodes ------------------------------------------------------------------


class Node:
    """A GenICam node.

    Nodes on the camera's own node map read as ``NA`` until the camera is
    initialized; transport-layer nodes are always available.
    """

    def __init__(
        self,
        camera: "FakeCamera",
        name: str,
        access: int | Callable[[], int] = RW,
        requires_init: bool = True,
    ) -> None:
        self._camera = camera
        self._name = name
        self._access = access
        self._requires_init = requires_init

    def GetName(self) -> str:
        return self._name

    def GetAccessMode(self) -> int:
        if self._requires_init and not self._camera.initialized:
            return NA
        return self._access() if callable(self._access) else self._access

    def _check(self, modes: tuple[int, ...], verb: str) -> None:
        if not self._camera.attached:
            raise SpinnakerException(f"{self._name}: device lost", SPINNAKER_ERR_IO)
        if self.GetAccessMode() not in modes:
            raise SpinnakerException(
                f"{self._name} is not {verb}", SPINNAKER_ERR_ACCESS_DENIED
            )

    def _check_read(self) -> None:
        self._check((RO, RW), "readable")

    def _check_write(self) -> None:
        self._check((WO, RW), "writable")


class ValueNode(Node):
    def __init__(self, camera: "FakeCamera", name: str, value: Any, **kw: Any):
        super().__init__(camera, name, **kw)
        self._value = value

    def GetValue(self) -> Any:
        self._check_read()
        return self._value

    def SetValue(self, value: Any) -> None:
        self._check_write()
        self._validate(value)
        self._value = value

    def ToString(self) -> str:
        return str(self.GetValue())

    def _validate(self, value: Any) -> None:
        return


class StringNode(ValueNode):
    pass


class BooleanNode(ValueNode):
    pass


class FloatNode(ValueNode):
    def __init__(
        self,
        camera: "FakeCamera",
        name: str,
        value: float,
        minimum: float,
        maximum: float,
        unit: str = "",
        **kw: Any,
    ) -> None:
        super().__init__(camera, name, value, **kw)
        self.minimum = minimum
        self.maximum = maximum
        self._unit = unit

    def GetMin(self) -> float:
        self._check_read()
        return self.minimum

    def GetMax(self) -> float:
        self._check_read()
        return self.maximum

    def GetUnit(self) -> str:
        return self._unit

    def _validate(self, value: Any) -> None:
        if not self.minimum <= value <= self.maximum:
            raise SpinnakerException(
                f"{self._name}: {value} is outside [{self.minimum}, {self.maximum}]",
                SPINNAKER_ERR_INVALID_PARAMETER,
            )


class IntegerNode(FloatNode):
    """An integer node. Like GenICam, it refuses values off its increment."""

    def __init__(
        self, camera: "FakeCamera", name: str, value: int, inc: int = 1, **kw: Any
    ):
        kw.setdefault("minimum", value)
        kw.setdefault("maximum", value)
        super().__init__(camera, name, value, **kw)
        self.inc = inc

    def GetInc(self) -> int:
        return self.inc

    def _validate(self, value: Any) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise SpinnakerException(
                f"{self._name} takes an int, not {type(value).__name__}",
                SPINNAKER_ERR_INVALID_PARAMETER,
            )
        super()._validate(value)
        if (value - self.minimum) % self.inc:
            raise SpinnakerException(
                f"{self._name}: {value} is off the increment {self.inc}",
                SPINNAKER_ERR_INVALID_PARAMETER,
            )


class ComputedNode(Node):
    """A read-only node whose value the fake works out when read."""

    def __init__(
        self, camera: "FakeCamera", name: str, compute: Callable[[], Any], **kw: Any
    ) -> None:
        super().__init__(camera, name, access=RO, **kw)
        self._compute = compute

    def GetValue(self) -> Any:
        self._check_read()
        return self._compute()

    def ToString(self) -> str:
        return str(self.GetValue())


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

    def __init__(
        self,
        camera: "FakeCamera",
        name: str,
        value: str,
        entries: Iterable[str],
        **kw: Any,
    ) -> None:
        self._entries = {
            symbolic: EnumEntry(name, symbolic, _ENUMS[name].index(symbolic))
            for symbolic in entries
        }
        super().__init__(camera, name, self._entries[value].value, **kw)

    @property
    def symbolic(self) -> str:
        """The current entry's name, read without access checks."""
        return _ENUMS[self._name][self._value]

    def GetIntValue(self) -> int:
        return int(self.GetValue())

    def SetIntValue(self, value: int) -> None:
        self.SetValue(value)

    def GetCurrentEntry(self) -> EnumEntry:
        self._check_read()
        return self._entries[self.symbolic]

    def GetEntries(self) -> list[EnumEntry]:
        return list(self._entries.values())

    def GetEntryByName(self, symbolic: str) -> EnumEntry | None:
        return self._entries.get(symbolic)

    def ToString(self) -> str:
        return self.GetCurrentEntry().symbolic

    def _validate(self, value: Any) -> None:
        if value not in {entry.value for entry in self._entries.values()}:
            raise SpinnakerException(
                f"{self._name}: no entry with value {value}",
                SPINNAKER_ERR_INVALID_PARAMETER,
            )


class CommandNode(Node):
    def __init__(
        self, camera: "FakeCamera", name: str, action: Callable[[], None], **kw: Any
    ) -> None:
        super().__init__(camera, name, access=WO, **kw)
        self._action = action

    def Execute(self) -> None:
        self._check_write()
        self._action()

    def IsDone(self) -> bool:
        return True


class NodeMap:
    def __init__(self, nodes: dict[str, Node]) -> None:
        self._nodes = nodes

    def GetNode(self, name: str) -> Node | None:
        return self._nodes.get(name)

    def GetNodes(self) -> list[Node]:
        return list(self._nodes.values())


class _NodeAttributes:
    """QuickSpin-style access, e.g. ``cam.TLDevice.DeviceSerialNumber``."""

    def __init__(self, nodes: dict[str, Node]) -> None:
        self._nodes = nodes

    def __getattr__(self, name: str) -> Any:
        try:
            return self._nodes[name]
        except KeyError:
            raise AttributeError(name) from None


def IsAvailable(node: Node | None) -> bool:
    return node is not None and node.GetAccessMode() not in (NI, NA)


def IsImplemented(node: Node | None) -> bool:
    return node is not None and node.GetAccessMode() != NI


def IsReadable(node: Node | None) -> bool:
    return node is not None and node.GetAccessMode() in (RO, RW)


def IsWritable(node: Node | None) -> bool:
    return node is not None and node.GetAccessMode() in (WO, RW)


def _cast(node: Node | None) -> Node | None:
    return node


class EnumerationPtr:
    """What ``PySpin.CEnumerationPtr(node)`` gives: an enumeration without the
    ``GetValue``/``SetValue`` that QuickSpin attributes (``cam.PixelFormat``) have.
    """

    def __init__(self, node: "EnumNode") -> None:
        self._node = node

    def GetAccessMode(self) -> int:
        return self._node.GetAccessMode()

    def GetName(self) -> str:
        return self._node.GetName()

    def GetIntValue(self) -> int:
        return self._node.GetIntValue()

    def SetIntValue(self, value: int) -> None:
        self._node.SetIntValue(value)

    def GetCurrentEntry(self) -> "EnumEntry":
        return self._node.GetCurrentEntry()

    def GetEntries(self) -> "list[EnumEntry]":
        return self._node.GetEntries()

    def GetEntryByName(self, symbolic: str) -> "EnumEntry | None":
        return self._node.GetEntryByName(symbolic)

    def ToString(self) -> str:
        return self._node.ToString()


def _enumeration(node: Node | None) -> EnumerationPtr | None:
    if node is None:
        return None
    if not isinstance(node, EnumNode):
        raise SpinnakerException(f"{node.GetName()} is not an enumeration")
    return EnumerationPtr(node)


class Image:
    """Stands in for ``PySpin.Image``, which holds the status descriptions."""

    @staticmethod
    def GetImageStatusDescription(status: int) -> str:
        name = next(n for n, v in _IMAGE_STATUSES.items() if v == status)
        return name.removeprefix("SPINNAKER_IMAGE_STATUS_").replace("_", " ").lower()


# --- Images -----------------------------------------------------------------


@dataclass
class FrameSpec:
    """A frame to hand out from ``GetNextImage()``.

    Attributes:
        timestamp_ns: Device timestamp. A frame with one is already waiting in a
            buffer and needs no trigger. ``None`` makes a fresh frame whose
            exposure starts when it is triggered (or retrieved, outside trigger
            mode), stamped at the end of that exposure, as real cameras do.
        status: Image status; anything but ``SPINNAKER_IMAGE_STATUS_NO_ERROR`` is incomplete.
    """

    timestamp_ns: int | None = None
    status: int = SPINNAKER_IMAGE_STATUS_NO_ERROR


class FakeImage:
    """A grabbed buffer. It stays held until ``Release()``."""

    # Written over a buffer's pixels when it is released.
    RELEASED_FILL = 0xAB

    def __init__(
        self,
        camera: "FakeCamera",
        frame_id: int,
        timestamp_ns: int,
        status: int,
        pixel_format: str,
    ) -> None:
        dtype, channels = _LAYOUTS[pixel_format]
        shape: tuple[int, ...] = (camera.height, camera.width)
        if channels > 1:
            shape += (channels,)
        self._camera = camera
        self._buffer = np.full(shape, frame_id % 200, dtype=dtype)
        self._frame_id = frame_id
        self._timestamp_ns = timestamp_ns
        self._status = status
        self._pixel_format = pixel_format
        self.released = False

    def _check_held(self) -> None:
        if self.released:
            raise SpinnakerException("image was released", SPINNAKER_ERR_INVALID_HANDLE)

    def GetNDArray(self) -> np.ndarray[Any, Any]:
        self._check_held()
        return self._buffer.view()

    def GetTimeStamp(self) -> int:
        self._check_held()
        return self._timestamp_ns

    def GetFrameID(self) -> int:
        self._check_held()
        return self._frame_id

    def GetImageStatus(self) -> int:
        self._check_held()
        return self._status

    def IsIncomplete(self) -> bool:
        self._check_held()
        return self._status != SPINNAKER_IMAGE_STATUS_NO_ERROR

    def GetPixelFormat(self) -> int:
        return _ENUMS["PixelFormat"].index(self._pixel_format)

    def GetPixelFormatName(self) -> str:
        return self._pixel_format

    def GetWidth(self) -> int:
        return self._buffer.shape[1]

    def GetHeight(self) -> int:
        return self._buffer.shape[0]

    def IsInUse(self) -> bool:
        return not self.released

    def Release(self) -> None:
        self._check_held()
        self.released = True
        self._buffer.fill(self.RELEASED_FILL)
        self._camera.held.remove(self)


# --- Cameras ----------------------------------------------------------------


class FakeCamera:
    """The hardware behind a ``CameraPtr``, with knobs and counters for tests.

    Args:
        serial: Serial number, as reported by the transport layer.
        model: ``DeviceModelName``.
        vendor: ``DeviceVendorName``.
        color: Whether the sensor has a Bayer filter, which adds color formats.
        width: Image width in pixels.
        height: Image height in pixels.
        buffer_count: Buffers in the stream; ``GetNextImage()`` times out once
            all of them are held.
        clock: Host clock, in seconds since the epoch.
        uptime: Seconds the camera's timestamp counter has been running.
        timestamp_node: ``"Timestamp"`` or the older ``"TimestampLatchValue"``.
        firmware: ``DeviceFirmwareVersion``.
        formats: ``PixelFormat`` entries, first one current. Defaults to the
            usual mono or color set.
        auto: Whether auto exposure and auto gain start on.
        throughput: Whether the camera has ``DeviceLinkThroughputLimit``, with
            ``DeviceLinkCurrentThroughput`` and ``AcquisitionResultingFrameRate``.
        throughput_mode: Whether it also has ``DeviceLinkThroughputLimitMode``,
            starting Off; the limit is read-only until the mode is On.
        throughput_while_streaming: Whether the limit is writable while acquiring.
    """

    def __init__(
        self,
        serial: str = "20000001",
        model: str = "Blackfly S BFS-U3-51S5M",
        vendor: str = "FLIR",
        *,
        color: bool = False,
        width: int = 8,
        height: int = 6,
        buffer_count: int = 10,
        clock: Callable[[], float] = time.time,
        uptime: float = 1000.0,
        timestamp_node: str = "Timestamp",
        firmware: str = "1.0.0.0",
        formats: Iterable[str] | None = None,
        auto: bool = True,
        throughput: bool = True,
        throughput_mode: bool = False,
        throughput_while_streaming: bool = True,
    ) -> None:
        self.serial = serial
        self.model = model
        self.vendor = vendor
        self.width = width
        self.height = height
        self.buffer_count = buffer_count
        self.clock = clock
        self.device_epoch = clock() - uptime
        self.attached = False
        self.initialized = False
        self.streaming = False
        self.busy = False
        self.queue: deque[FrameSpec] = deque()
        self.held: list[FakeImage] = []
        self.frames_grabbed = 0
        self.init_calls = 0
        self.deinit_calls = 0
        self.latch_calls = 0
        self.trigger_calls = 0
        self.grab_timeouts: list[int] = []
        self.pending_triggers: deque[int] = deque()
        self.respond_to_triggers = True
        # Triggers to ignore before answering again, as a real camera ignores
        # one it isn't ready for.
        self.ignore_triggers = 0

        if formats is None:
            formats = ["Mono8", "Mono16"]
            if color:
                formats = ["RGB8", "BayerRG8", "BayerRG16", *formats]
        formats = list(formats)

        def unless_streaming() -> int:
            return RO if self.streaming else RW

        def node_map(*nodes: Node) -> dict[str, Node]:
            return {node.GetName(): node for node in nodes}

        identity = (
            StringNode(self, "DeviceSerialNumber", serial, access=RO),
            StringNode(self, "DeviceModelName", model, access=RO),
            StringNode(self, "DeviceVendorName", vendor, access=RO),
        )
        self.tl_device_nodes = node_map(
            *(
                StringNode(self, n.GetName(), n._value, access=RO, requires_init=False)
                for n in identity
            )
        )
        self.tl_stream_nodes = node_map(
            EnumNode(
                self,
                "StreamBufferHandlingMode",
                "OldestFirst",
                _ENUMS["StreamBufferHandlingMode"],
                requires_init=False,
            )
        )
        auto_mode = "Continuous" if auto else "Off"
        self.exposure_auto = EnumNode(
            self, "ExposureAuto", auto_mode, _ENUMS["ExposureAuto"]
        )
        self.gain_auto = EnumNode(self, "GainAuto", auto_mode, _ENUMS["GainAuto"])
        self.exposure = exposure = FloatNode(
            self,
            "ExposureTime",
            10_000.0,
            minimum=6.0,
            maximum=30_000_000.0,
            unit="us",
            access=lambda: RW if self.exposure_auto.symbolic == "Off" else RO,
        )
        gain = FloatNode(
            self,
            "Gain",
            0.0,
            minimum=0.0,
            maximum=47.99,
            unit="dB",
            access=lambda: RW if self.gain_auto.symbolic == "Off" else RO,
        )
        self.trigger_mode = EnumNode(self, "TriggerMode", "Off", _ENUMS["TriggerMode"])
        self.trigger_source = EnumNode(
            self,
            "TriggerSource",
            "Line0",
            _ENUMS["TriggerSource"],
            access=lambda: RW if self.trigger_mode.symbolic == "Off" else RO,
        )
        self.trigger_selector = EnumNode(
            self, "TriggerSelector", "FrameStart", _ENUMS["TriggerSelector"]
        )
        self.pixel_format = EnumNode(
            self, "PixelFormat", formats[0], formats, access=unless_streaming
        )
        self.nodes = node_map(
            *identity,
            StringNode(self, "DeviceFirmwareVersion", firmware, access=RO),
            IntegerNode(self, "Width", width, access=RO),
            IntegerNode(self, "Height", height, access=RO),
            self.pixel_format,
            EnumNode(
                self,
                "PixelColorFilter",
                "BayerRG" if color else "None",
                _ENUMS["PixelColorFilter"],
                access=RO,
            ),
            EnumNode(
                self,
                "AcquisitionMode",
                "Continuous",
                _ENUMS["AcquisitionMode"],
                access=unless_streaming,
            ),
            EnumNode(self, "ExposureMode", "Timed", _ENUMS["ExposureMode"]),
            self.exposure_auto,
            exposure,
            self.gain_auto,
            gain,
            self.trigger_selector,
            self.trigger_mode,
            self.trigger_source,
            CommandNode(self, "TriggerSoftware", self._trigger),
            CommandNode(self, "TimestampLatch", self._latch),
            IntegerNode(
                self,
                timestamp_node,
                0,
                maximum=2**63 - 1,
                access=RO,
            ),
        )
        self._timestamp = self.nodes[timestamp_node]
        if throughput:
            self._add_throughput_nodes(throughput_mode, throughput_while_streaming)

    # Made-up figures, roughly a USB3 camera; nothing tests them against hardware.
    MAX_FRAME_RATE = 35.0
    THROUGHPUT_MIN = 10_000_000
    THROUGHPUT_MAX = 500_000_000
    THROUGHPUT_INC = 8_000

    def _add_throughput_nodes(self, with_mode: bool, while_streaming: bool) -> None:
        mode = EnumNode(
            self,
            "DeviceLinkThroughputLimitMode",
            "Off",
            _ENUMS["DeviceLinkThroughputLimitMode"],
        )

        def limit_access() -> int:
            if with_mode and mode.symbolic != "On":
                return RO
            if self.streaming and not while_streaming:
                return RO
            return RW

        limit = IntegerNode(
            self,
            "DeviceLinkThroughputLimit",
            self.THROUGHPUT_MAX,
            minimum=self.THROUGHPUT_MIN,
            maximum=self.THROUGHPUT_MAX,
            inc=self.THROUGHPUT_INC,
            unit="Bps",
            access=limit_access,
        )
        frame_bytes = self.width * self.height

        def current() -> int:
            if not self.streaming:
                return 0
            active = not with_mode or mode.symbolic == "On"
            cap = limit._value if active else self.THROUGHPUT_MAX
            return min(cap, round(self.MAX_FRAME_RATE * frame_bytes))

        def frame_rate() -> float:
            active = not with_mode or mode.symbolic == "On"
            cap = limit._value if active else self.THROUGHPUT_MAX
            return min(self.MAX_FRAME_RATE, cap / frame_bytes)

        self.throughput_limit = limit
        self.throughput_limit_mode = mode if with_mode else None
        nodes = [
            limit,
            ComputedNode(self, "DeviceLinkCurrentThroughput", current),
            ComputedNode(self, "AcquisitionResultingFrameRate", frame_rate),
        ]
        if with_mode:
            nodes.append(mode)
        self.nodes.update({node.GetName(): node for node in nodes})

    def device_time_ns(self) -> int:
        """The camera's timestamp counter now, in nanoseconds."""
        return round((self.clock() - self.device_epoch) * 1e9)

    def queue_frames(self, *frames: FrameSpec) -> None:
        """Hand out these frames, in order, before any fresh ones."""
        self.queue.extend(frames)

    def _latch(self) -> None:
        self.latch_calls += 1
        self._timestamp._value = self.device_time_ns()  # type: ignore[attr-defined]

    @property
    def triggered(self) -> bool:
        """Whether frames wait for a software trigger."""
        return (
            self.trigger_mode.symbolic == "On"
            and self.trigger_source.symbolic == "Software"
        )

    def _trigger(self) -> None:
        if not (self.streaming and self.triggered):
            raise SpinnakerException(
                f"camera {self.serial} isn't acquiring in software-trigger mode",
                SPINNAKER_ERR_ACCESS_DENIED,
            )
        self.trigger_calls += 1
        if self.ignore_triggers:
            self.ignore_triggers -= 1
        elif self.respond_to_triggers:
            self.pending_triggers.append(self.device_time_ns())

    def _require(self, initialized: bool = True) -> None:
        if not self.attached:
            raise SpinnakerException(f"camera {self.serial} lost", SPINNAKER_ERR_IO)
        if initialized and not self.initialized:
            raise SpinnakerException(
                f"camera {self.serial} is not initialized",
                SPINNAKER_ERR_NOT_INITIALIZED,
            )

    def _next_image(self) -> FakeImage:
        self._require()
        if not self.streaming:
            raise SpinnakerException(
                f"camera {self.serial} is not acquiring", SPINNAKER_ERR_ERROR
            )
        # Real PySpin blocks until the grab timeout; failing at once keeps
        # tests fast.
        if len(self.held) >= self.buffer_count:
            raise SpinnakerException(
                f"camera {self.serial}: no free buffers", SPINNAKER_ERR_TIMEOUT
            )
        waiting = self.queue and self.queue[0].timestamp_ns is not None
        if self.triggered and not waiting:
            if not self.pending_triggers:
                raise SpinnakerException(
                    f"camera {self.serial}: no frame was triggered",
                    SPINNAKER_ERR_TIMEOUT,
                )
            fresh_timestamp = self.pending_triggers.popleft()
        else:
            fresh_timestamp = self.device_time_ns()
        spec = self.queue.popleft() if self.queue else FrameSpec()
        timestamp = spec.timestamp_ns
        if timestamp is None:
            timestamp = fresh_timestamp + round(self.exposure._value * 1000)
        self.frames_grabbed += 1
        image = FakeImage(
            self,
            self.frames_grabbed,
            timestamp,
            spec.status,
            self.pixel_format.symbolic,
        )
        self.held.append(image)
        return image


class CameraPtr:
    """A reference to a camera, as handed out by a `CameraList`.

    The system counts live pointers, so a driver that keeps one (even as a
    leftover loop variable) cannot release the system.
    """

    def __init__(self, camera: FakeCamera | None, system: "FakeSystem") -> None:
        self._camera = camera
        if camera is not None:
            self.TLDevice = _NodeAttributes(camera.tl_device_nodes)
            self.TLStream = _NodeAttributes(camera.tl_stream_nodes)
        system._live_pointers.add(self)

    def __getattr__(self, name: str) -> Any:
        camera = self.__dict__.get("_camera")
        if camera is None or name not in camera.nodes:
            raise AttributeError(name)
        return camera.nodes[name]

    def _cam(self) -> FakeCamera:
        if self._camera is None:
            raise SpinnakerException(
                "invalid camera pointer", SPINNAKER_ERR_INVALID_HANDLE
            )
        return self._camera

    def IsValid(self) -> bool:
        return self._camera is not None

    def GetUniqueID(self) -> str:
        return self._cam().serial

    def Init(self) -> None:
        cam = self._cam()
        cam._require(initialized=False)
        if cam.busy:
            raise SpinnakerException(
                f"camera {cam.serial} is in use by another process",
                SPINNAKER_ERR_ACCESS_DENIED,
            )
        cam.init_calls += 1
        cam.initialized = True

    def DeInit(self) -> None:
        cam = self._cam()
        if cam.streaming:
            raise SpinnakerException(
                f"camera {cam.serial} is still acquiring", SPINNAKER_ERR_RESOURCE_IN_USE
            )
        cam.deinit_calls += 1
        cam.initialized = False

    def IsInitialized(self) -> bool:
        return self._cam().initialized

    def IsStreaming(self) -> bool:
        return self._cam().streaming

    def BeginAcquisition(self) -> None:
        cam = self._cam()
        cam._require()
        if cam.streaming:
            raise SpinnakerException(
                f"camera {cam.serial} is already acquiring",
                SPINNAKER_ERR_RESOURCE_IN_USE,
            )
        cam.streaming = True

    def EndAcquisition(self) -> None:
        cam = self._cam()
        if not cam.streaming:
            raise SpinnakerException(
                f"camera {cam.serial} is not acquiring", SPINNAKER_ERR_ERROR
            )
        cam.streaming = False

    def GetNextImage(self, grabTimeout: int = EVENT_TIMEOUT_INFINITE) -> FakeImage:
        cam = self._cam()
        cam.grab_timeouts.append(grabTimeout)
        return cam._next_image()

    def GetNodeMap(self) -> NodeMap:
        return NodeMap(self._cam().nodes)

    def GetTLDeviceNodeMap(self) -> NodeMap:
        return NodeMap(self._cam().tl_device_nodes)

    def GetTLStreamNodeMap(self) -> NodeMap:
        return NodeMap(self._cam().tl_stream_nodes)


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

    def __init__(self) -> None:
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
        initialized = [c.serial for c in self.cameras if c.initialized]
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
        self.system = FakeSystem()
        self.System = self.system
        for camera in cameras:
            self.attach(camera)

    def attach(self, camera: FakeCamera) -> FakeCamera:
        """Plug a camera in."""
        camera.attached = True
        self.system.cameras.append(camera)
        return camera

    def detach(self, serial: str) -> FakeCamera:
        """Unplug a camera. Existing pointers to it start failing."""
        camera = next(c for c in self.system.cameras if c.serial == serial)
        camera.attached = False
        camera.initialized = False
        camera.streaming = False
        self.system.cameras.remove(camera)
        return camera
