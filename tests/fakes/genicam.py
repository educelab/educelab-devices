"""A fake GenICam camera, and an in-memory backend that reaches it.

`FakeCamera` simulates an SFNC camera independently of any library: its node
map, acquisition, software trigger, timestamp latch, and buffer pool. It
raises `FakeCameraError` with a `kind` that each library's fake translates
into that library's errors. `FakeBackend` puts it behind the seam in
`cameras._genicam` directly, so the shared core can be tested on its own.

Like ``fakes.pyspin``, it is strict where real cameras punish mistakes:
frames in trigger mode need a ``TriggerSoftware``, ``TriggerSource`` is
writable only while ``TriggerMode`` is off, ``PixelFormat`` only while
stopped, and once every buffer is held a grab times out. Released buffers'
pixels are overwritten, so arrays used without a copy show garbage.
"""

import math
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

from educelab.devices.cameras import _genicam
from educelab.devices.cameras._genicam import BufferStatus
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import DeviceBusy, DeviceError, DeviceTimeout

__all__ = [
    "PFNC",
    "FakeBackend",
    "FakeBuffer",
    "FakeCamera",
    "FakeCameraError",
    "FakeConnection",
    "FakeFrame",
    "FrameSpec",
    "Node",
    "frame_bytes",
    "payload_size",
]

Kind = Literal["float", "int", "string", "enum", "command"]
Access = Literal["RW", "RO", "WO", "NA"]
ErrorKind = Literal["not_found", "access", "range", "timeout", "state", "busy"]

ENUMS = {
    "ExposureAuto": ("Off", "Once", "Continuous"),
    "GainAuto": ("Off", "Once", "Continuous"),
    "ExposureMode": ("Timed", "TriggerWidth"),
    "AcquisitionMode": ("Continuous", "SingleFrame", "MultiFrame"),
    "TriggerMode": ("Off", "On"),
    "TriggerSelector": ("FrameStart", "AcquisitionStart", "FrameBurstStart"),
    "TriggerSource": ("Software", "Line0", "Line1", "Line2", "Line3"),
    "DeviceLinkThroughputLimitMode": ("Off", "On"),
}

# (dtype, channels) of each pixel format's array.
LAYOUTS: dict[str, tuple[type[np.unsignedinteger[Any]], int]] = {
    "Mono8": (np.uint8, 1),
    "Mono16": (np.uint16, 1),
    "RGB8": (np.uint8, 3),
    "BayerRG8": (np.uint8, 1),
    "BayerRG16": (np.uint16, 1),
    "Mono12Packed": (np.uint8, 1),
    # Unpacked here; library fakes pack it as the camera sends it.
    "Mono10g40IDS": (np.uint16, 1),
}


# PFNC codes of the formats above, for library fakes that report numbers.
PFNC = {
    "Mono8": 0x01080001,
    "Mono16": 0x01100007,
    "RGB8": 0x02180014,
    "BayerRG8": 0x01080009,
    "BayerRG16": 0x0110002F,
    "Mono12Packed": 0x010C0006,
    "Mono10g40IDS": 0x4000000F,
}


def frame_bytes(pixels: npt.NDArray[Any], pixel_format: str) -> bytes:
    """A frame's pixels as the camera sends them: packed for Mono10g40IDS."""
    if pixel_format != "Mono10g40IDS":
        return pixels.tobytes()
    groups = pixels.ravel().reshape(-1, 4).astype(np.uint16)
    upper = (groups >> 2).astype(np.uint8)
    low = np.zeros(len(groups), dtype=np.uint8)
    for i in range(4):
        low |= ((groups[:, i] & 3) << (2 * i)).astype(np.uint8)
    return np.column_stack([upper, low]).tobytes()


def payload_size(sim: "FakeCamera") -> int:
    """Bytes in one frame in the camera's current pixel format."""
    fmt = sim.get("PixelFormat")
    per_pixel = {"Mono16": 2, "BayerRG16": 2, "RGB8": 3, "Mono10g40IDS": 1.25}
    return int(sim.width * sim.height * per_pixel.get(fmt, 1))


class FakeCameraError(Exception):
    """A library-neutral failure, translated by each library's fake."""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class Node:
    """One node of a fake camera's node map, in its native units."""

    name: str
    kind: Kind
    value: Any = None
    minimum: float | None = None
    maximum: float | None = None
    inc: int = 1
    entries: tuple[str, ...] = ()
    access: Access | Callable[[], Access] = "RW"
    action: Callable[[], None] | None = None
    compute: Callable[[], Any] | None = None

    def current_access(self) -> Access:
        return self.access() if callable(self.access) else self.access


@dataclass(frozen=True)
class FrameSpec:
    """A frame to hand out next, instead of a fresh one.

    Attributes:
        timestamp_ns: The device timestamp, or None to stamp it as a fresh
            frame (end of exposure, now or at the pending trigger).
        status: Whether the frame arrived whole.
    """

    timestamp_ns: int | None = None
    status: BufferStatus = BufferStatus.COMPLETE


@dataclass(eq=False)
class FakeFrame:
    """A grabbed frame, held in one of the camera's buffers until released."""

    camera: "FakeCamera"
    frame_id: int
    timestamp_ns: int
    status: BufferStatus
    pixel_format: str
    pixels: npt.NDArray[Any] = field(repr=False)
    arrival_ns: int | None = None
    released: bool = False

    # Written over a buffer's pixels when it is released.
    RELEASED_FILL = 0xAB

    def release(self) -> None:
        if self.released:
            raise FakeCameraError("state", "buffer was already released")
        self.released = True
        self.pixels.fill(self.RELEASED_FILL)
        self.camera.held.remove(self)


class FakeCamera:
    """A simulated SFNC camera, with knobs and counters for tests.

    Args:
        serial: ``DeviceSerialNumber``.
        model: ``DeviceModelName``.
        vendor: ``DeviceVendorName``.
        color: Whether the sensor has a Bayer filter, which adds color formats.
        width: Image width in pixels.
        height: Image height in pixels.
        buffer_count: Buffers in the stream; grabs time out once all are held.
        clock: Host clock, in seconds since the epoch. Defaults to the
            core's `host_time()`, which is fine-grained on every platform.
        uptime: Seconds the camera's timestamp counter has been running.
        timestamp_node: ``"Timestamp"`` or the older ``"TimestampLatchValue"``.
        exposure_node: ``"ExposureTime"`` or the older ``"ExposureTimeAbs"``.
        firmware: ``DeviceFirmwareVersion``.
        formats: ``PixelFormat`` entries, first one current. Defaults to the
            usual mono or color set.
        auto: Whether auto exposure and auto gain start on.
        throughput_mode: Whether ``DeviceLinkThroughputLimitMode`` exists,
            starting Off; the limit is read-only until the mode is On.
        throughput_while_streaming: Whether the limit is writable while acquiring.
        without: Names of nodes the camera lacks, e.g. ``["TriggerSoftware"]``.
        arrival_latency: Seconds from a frame's device timestamp to the host
            seeing it arrive, or None if the library reports no arrival time.
        ready_after: In trigger mode, stamp each frame this many seconds after
            the previous frame's trigger, when the camera is ready for it, as
            the IDS U3-36PxXCP stamps a frame's leader before its own trigger.
            None stamps frames at the end of their exposure.
        busy_after_frame: Seconds after handing out a frame during which the
            camera ignores triggers, as the IDS U3-36PxXCP does for about 3 ms.
    """

    # Made-up figures, roughly a USB3 camera; nothing tests them against hardware.
    MAX_FRAME_RATE = 35.0
    THROUGHPUT_MIN = 10_000_000
    THROUGHPUT_MAX = 500_000_000
    THROUGHPUT_INC = 8_000

    def __init__(
        self,
        serial: str = "4108000001",
        model: str = "U3-36P0XCP-M",
        *,
        vendor: str = "IDS Imaging",
        color: bool = False,
        width: int = 8,
        height: int = 6,
        buffer_count: int = 10,
        clock: Callable[[], float] | None = None,
        uptime: float = 1000.0,
        timestamp_node: str = "Timestamp",
        exposure_node: str = "ExposureTime",
        firmware: str = "1.0.0",
        formats: Iterable[str] | None = None,
        auto: bool = True,
        throughput_mode: bool = False,
        throughput_while_streaming: bool = True,
        without: Iterable[str] = (),
        arrival_latency: float | None = None,
        ready_after: float | None = None,
        busy_after_frame: float = 0.0,
    ) -> None:
        self.arrival_latency = arrival_latency
        self.ready_after = ready_after
        self.last_trigger_ns: int | None = None
        self.busy_after_frame = busy_after_frame
        self.frame_handed_out_at = -math.inf
        self.serial = serial
        self.model = model
        self.vendor = vendor
        self.width = width
        self.height = height
        self.buffer_count = buffer_count
        self.clock = clock or (lambda: _genicam.host_time())
        self.device_epoch = self.clock() - uptime
        self.timestamp_node = timestamp_node
        self.exposure_node = exposure_node
        self.attached = True
        self.busy = False
        self.opened = False
        self.streaming = False
        self.queue: deque[FrameSpec] = deque()
        self.held: list[FakeFrame] = []
        self.pending_triggers: deque[int] = deque()
        self.frames_grabbed = 0
        self.open_calls = 0
        self.latch_calls = 0
        self.latched_ns = 0
        self.trigger_calls = 0
        # Triggers to ignore before answering again, as a real camera ignores
        # one it isn't ready for.
        self.ignore_triggers = 0
        self.ignored_while_busy = 0
        self.respond_to_triggers = True
        # Operations to fail, e.g. {"write Gain": FakeCameraError(...)}. Keys are
        # "read <node>", "write <node>", "execute <node>", "start", "stop", "close".
        self.failures: dict[str, FakeCameraError] = {}

        if formats is None:
            formats = ["Mono8", "Mono16", "Mono12Packed"]
            if color:
                formats = ["RGB8", "BayerRG8", "BayerRG16", *formats]
        formats = tuple(formats)
        auto_mode = "Continuous" if auto else "Off"

        def unless_streaming() -> Access:
            return "RO" if self.streaming else "RW"

        def when_off(name: str) -> Callable[[], Access]:
            # A camera without the switch (e.g. no ExposureAuto) is always manual.
            return lambda: (
                "RW" if not self.has(name) or self.get(name) == "Off" else "RO"
            )

        def limit_access() -> Access:
            if throughput_mode and self.get("DeviceLinkThroughputLimitMode") != "On":
                return "RO"
            if self.streaming and not throughput_while_streaming:
                return "RO"
            return "RW"

        def limit_cap() -> float:
            if not self.has("DeviceLinkThroughputLimit"):
                return self.THROUGHPUT_MAX
            on = (
                not throughput_mode or self.get("DeviceLinkThroughputLimitMode") == "On"
            )
            return self.get("DeviceLinkThroughputLimit") if on else self.THROUGHPUT_MAX

        def frame_rate() -> float:
            return min(self.MAX_FRAME_RATE, limit_cap() / (width * height))

        def current_throughput() -> int:
            if not self.streaming:
                return 0
            return round(min(limit_cap(), self.MAX_FRAME_RATE * width * height))

        def enum(name: str, value: str, **kwargs: Any) -> Node:
            return Node(name, "enum", value, entries=ENUMS[name], **kwargs)

        nodes = [
            Node("DeviceSerialNumber", "string", serial, access="RO"),
            Node("DeviceModelName", "string", model, access="RO"),
            Node("DeviceVendorName", "string", vendor, access="RO"),
            Node("DeviceFirmwareVersion", "string", firmware, access="RO"),
            Node("Width", "int", width, width, width, access="RO"),
            Node("Height", "int", height, height, height, access="RO"),
            Node(
                "PixelFormat",
                "enum",
                formats[0],
                entries=formats,
                access=unless_streaming,
            ),
            enum("AcquisitionMode", "Continuous", access=unless_streaming),
            enum("ExposureMode", "Timed"),
            enum("ExposureAuto", auto_mode),
            Node(
                exposure_node,
                "float",
                10_000.0,
                minimum=6.0,
                maximum=30_000_000.0,
                access=when_off("ExposureAuto"),
            ),
            enum("GainAuto", auto_mode),
            Node(
                "Gain",
                "float",
                0.0,
                minimum=0.0,
                maximum=47.99,
                access=when_off("GainAuto"),
            ),
            enum("TriggerSelector", "FrameStart"),
            enum("TriggerMode", "Off"),
            enum("TriggerSource", "Line0", access=when_off("TriggerMode")),
            Node("TriggerSoftware", "command", access="WO", action=self._trigger),
            # What a GenTL consumer executes on the camera itself; PySpin and
            # Aravis do it for the driver.
            Node("AcquisitionStart", "command", access="WO", action=self.start),
            Node("AcquisitionStop", "command", access="WO", action=self.stop),
            Node("TLParamsLocked", "int", 0, 0, 1, access="RW"),
            Node("TimestampLatch", "command", access="WO", action=self._latch),
            Node(
                timestamp_node,
                "int",
                minimum=0,
                maximum=2**63 - 1,
                access="RO",
                compute=lambda: self.latched_ns,
            ),
            Node(
                "DeviceLinkThroughputLimit",
                "int",
                self.THROUGHPUT_MAX,
                minimum=self.THROUGHPUT_MIN,
                maximum=self.THROUGHPUT_MAX,
                inc=self.THROUGHPUT_INC,
                access=limit_access,
            ),
            Node(
                "DeviceLinkCurrentThroughput",
                "int",
                access="RO",
                compute=current_throughput,
            ),
            Node(
                "AcquisitionResultingFrameRate",
                "float",
                access="RO",
                compute=frame_rate,
            ),
        ]
        if throughput_mode:
            nodes.append(enum("DeviceLinkThroughputLimitMode", "Off"))
        dropped = set(without)
        self.nodes = {n.name: n for n in nodes if n.name not in dropped}

    def __repr__(self) -> str:
        return f"<FakeCamera {self.serial}>"

    # --- Test knobs (no access checks) ------------------------------------------

    def get(self, name: str) -> Any:
        node = self._node(name)
        return node.compute() if node.compute else node.value

    def set(self, name: str, value: Any) -> None:
        self._node(name).value = value

    def range(self, name: str) -> tuple[Any, ...]:
        node = self._node(name)
        if node.kind == "int":
            return (node.minimum, node.maximum, node.inc)
        return (node.minimum, node.maximum)

    def entries(self, name: str) -> tuple[str, ...]:
        return self._node(name).entries

    def device_time_ns(self) -> int:
        """The camera's timestamp counter now, in nanoseconds."""
        return round((self.clock() - self.device_epoch) * 1e9)

    def queue_frames(self, *frames: FrameSpec) -> None:
        """Hand out these frames, in order, before any fresh ones."""
        self.queue.extend(frames)

    @property
    def triggered(self) -> bool:
        """Whether frames wait for a software trigger."""
        if not (self.has("TriggerMode") and self.has("TriggerSource")):
            return False
        return self.get("TriggerMode") == "On" and self.get("TriggerSource") == (
            "Software"
        )

    # --- What a library sees ------------------------------------------------------

    def open(self) -> None:
        self._require_attached()
        if self.busy:
            raise FakeCameraError("busy", f"camera {self.serial} is held elsewhere")
        if self.opened:
            raise FakeCameraError("busy", f"camera {self.serial} is already open")
        self.open_calls += 1
        self.opened = True

    def close(self) -> None:
        self._maybe_fail("close")
        if self.streaming:
            raise FakeCameraError("state", f"camera {self.serial} is still acquiring")
        self.opened = False

    def has(self, name: str) -> bool:
        return name in self.nodes

    def access(self, name: str) -> Access:
        node = self.nodes.get(name)
        if node is None or not self.opened:
            return "NA"
        return node.current_access()

    def kind(self, name: str) -> Kind:
        return self._node(name).kind

    def read(self, name: str) -> Any:
        self._require_open()
        self._maybe_fail(f"read {name}")
        if self.access(name) not in ("RW", "RO"):
            raise FakeCameraError("access", f"{name} isn't readable")
        return self.get(name)

    def write(self, name: str, value: Any) -> None:
        self._require_open()
        node = self._node(name)
        if self.access(name) not in ("RW", "WO"):
            raise FakeCameraError("access", f"{name} isn't writable")
        self._maybe_fail(f"write {name}")
        if node.kind == "enum":
            if value not in node.entries:
                raise FakeCameraError("range", f"{name} has no entry {value!r}")
        elif node.kind in ("float", "int"):
            assert node.minimum is not None and node.maximum is not None
            if not node.minimum <= value <= node.maximum:
                raise FakeCameraError(
                    "range",
                    f"{name} {value} is outside {node.minimum}..{node.maximum}",
                )
            if node.kind == "int" and (value - node.minimum) % node.inc:
                raise FakeCameraError("range", f"{name} {value} is off its increment")
        node.value = value

    def execute(self, name: str) -> None:
        self._require_open()
        node = self._node(name)
        if node.kind != "command" or self.access(name) not in ("RW", "WO"):
            raise FakeCameraError("access", f"{name} can't be executed")
        self._maybe_fail(f"execute {name}")
        assert node.action is not None
        node.action()

    def start(self) -> None:
        self._require_open()
        self._maybe_fail("start")
        if self.streaming:
            raise FakeCameraError("state", f"camera {self.serial} is already acquiring")
        self.streaming = True

    def stop(self) -> None:
        self._maybe_fail("stop")
        if not self.streaming:
            raise FakeCameraError("state", f"camera {self.serial} isn't acquiring")
        self.streaming = False

    def frame_waiting(self) -> bool:
        """Whether `next_frame()` would return without waiting."""
        if not self.streaming or len(self.held) >= self.buffer_count:
            return False
        if self.queue and self.queue[0].timestamp_ns is not None:
            return True
        return not self.triggered or bool(self.pending_triggers)

    def next_frame(self) -> FakeFrame:
        """The next frame, or a ``timeout`` error if none would come.

        Real cameras block until the grab timeout; failing at once keeps tests
        fast.
        """
        self._require_open()
        if not self.streaming:
            raise FakeCameraError("state", f"camera {self.serial} isn't acquiring")
        if not self.frame_waiting():
            raise FakeCameraError("timeout", f"camera {self.serial}: no frame")
        waiting = self.queue and self.queue[0].timestamp_ns is not None
        if self.triggered and not waiting:
            fresh = self.pending_triggers.popleft()
        else:
            fresh = self.device_time_ns()
        spec = self.queue.popleft() if self.queue else FrameSpec()
        timestamp = spec.timestamp_ns
        if self.triggered and not waiting:
            previous, self.last_trigger_ns = self.last_trigger_ns, fresh
            if timestamp is None and self.ready_after is not None:
                ready = fresh if previous is None else previous
                timestamp = ready + round(self.ready_after * 1e9)
        if timestamp is None:
            exposure_us = (
                self.get(self.exposure_node) if self.has(self.exposure_node) else 0
            )
            timestamp = fresh + round(exposure_us * 1000)
        self.frames_grabbed += 1
        pixel_format = self.get("PixelFormat")
        dtype, channels = LAYOUTS[pixel_format]
        shape: tuple[int, ...] = (self.height, self.width)
        if channels > 1:
            shape += (channels,)
        arrival_ns = None
        if self.arrival_latency is not None:
            arrival_ns = round(
                (self.device_epoch + timestamp / 1e9 + self.arrival_latency) * 1e9
            )
        frame = FakeFrame(
            self,
            self.frames_grabbed,
            timestamp,
            spec.status,
            pixel_format,
            np.full(shape, self.frames_grabbed % 200, dtype=dtype),
            arrival_ns,
        )
        self.held.append(frame)
        self.frame_handed_out_at = time.perf_counter()
        return frame

    # --- Internals ----------------------------------------------------------------

    def _node(self, name: str) -> Node:
        try:
            return self.nodes[name]
        except KeyError:
            raise FakeCameraError("not_found", f"no node {name}") from None

    def _maybe_fail(self, operation: str) -> None:
        if operation in self.failures:
            raise self.failures[operation]

    def _require_attached(self) -> None:
        if not self.attached:
            raise FakeCameraError("state", f"camera {self.serial} was unplugged")

    def _require_open(self) -> None:
        self._require_attached()
        if not self.opened:
            raise FakeCameraError("state", f"camera {self.serial} isn't open")

    def _latch(self) -> None:
        self.latch_calls += 1
        self.latched_ns = self.device_time_ns()

    def _trigger(self) -> None:
        if not (self.streaming and self.triggered):
            raise FakeCameraError(
                "access", f"camera {self.serial} isn't acquiring in trigger mode"
            )
        self.trigger_calls += 1
        if time.perf_counter() - self.frame_handed_out_at < self.busy_after_frame:
            self.ignored_while_busy += 1
        elif self.ignore_triggers:
            self.ignore_triggers -= 1
        elif self.respond_to_triggers:
            self.pending_triggers.append(self.device_time_ns())


# --- The seam, straight onto FakeCamera -------------------------------------------

_ERRORS: dict[ErrorKind, type[DeviceError]] = {
    "timeout": DeviceTimeout,
    "busy": DeviceBusy,
}


def _translated(exc: FakeCameraError) -> DeviceError:
    return _ERRORS.get(exc.kind, DeviceError)(str(exc))


class FakeBuffer:
    """A `Buffer` over one `FakeFrame`."""

    def __init__(self, frame: FakeFrame) -> None:
        self._frame = frame

    def _held(self) -> FakeFrame:
        if self._frame.released:
            raise DeviceError("buffer was released")
        return self._frame

    def status(self) -> BufferStatus:
        return self._held().status

    def status_text(self) -> str:
        return self._held().status.value

    def device_timestamp_ns(self) -> int:
        return self._held().timestamp_ns

    def frame_id(self) -> int:
        return self._held().frame_id

    def host_timestamp_ns(self) -> int | None:
        return self._held().arrival_ns

    def pixel_format(self) -> str:
        return self._held().pixel_format

    def pixels(self) -> npt.NDArray[Any]:
        return self._held().pixels.copy()

    def release(self) -> None:
        try:
            self._frame.release()
        except FakeCameraError as exc:
            raise _translated(exc) from None


class FakeConnection:
    """A `Connection` straight onto a `FakeCamera`."""

    def __init__(self, camera: FakeCamera) -> None:
        self.camera = camera
        self.closed = False
        self.grab_timeouts: list[float | None] = []

    def _do(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except FakeCameraError as exc:
            raise _translated(exc) from None

    def has(self, name: str) -> bool:
        return self.camera.has(name)

    def is_readable(self, name: str) -> bool:
        return self.camera.access(name) in ("RW", "RO")

    def is_writable(self, name: str) -> bool:
        return self.camera.access(name) in ("RW", "WO")

    def get_float(self, name: str) -> float:
        return float(self._do(lambda: self.camera.read(name)))

    def get_int(self, name: str) -> int:
        return int(self._do(lambda: self.camera.read(name)))

    def get_string(self, name: str) -> str:
        return str(self._do(lambda: self.camera.read(name)))

    def get_enum(self, name: str) -> str:
        return str(self._do(lambda: self.camera.read(name)))

    def float_range(self, name: str) -> tuple[float, float]:
        low, high = self._do(lambda: self.camera.range(name))
        return float(low), float(high)

    def int_range(self, name: str) -> tuple[int, int, int]:
        low, high, inc = self._do(lambda: self.camera.range(name))
        return int(low), int(high), int(inc)

    def enum_entries(self, name: str) -> tuple[str, ...]:
        return tuple(self._do(lambda: self.camera.entries(name)))

    def set_float(self, name: str, value: float) -> None:
        self._do(lambda: self.camera.write(name, float(value)))

    def set_int(self, name: str, value: int) -> None:
        self._do(lambda: self.camera.write(name, int(value)))

    def set_enum(self, name: str, symbol: str) -> None:
        self._do(lambda: self.camera.write(name, symbol))

    def execute(self, name: str) -> None:
        self._do(lambda: self.camera.execute(name))

    def start_acquisition(self) -> None:
        self._do(self.camera.start)

    def stop_acquisition(self) -> None:
        if self.camera.streaming:
            self._do(self.camera.stop)

    def grab(self, timeout: float | None) -> FakeBuffer:
        self.grab_timeouts.append(timeout)
        return FakeBuffer(self._do(self.camera.next_frame))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        problems = []
        for step in (self.stop_acquisition, lambda: self._do(self.camera.close)):
            try:
                step()
            except DeviceError as exc:
                problems.append(str(exc))
        if problems:
            raise DeviceError("; ".join(problems))


class FakeBackend:
    """A `Backend` whose cameras are `FakeCamera`s.

    Args:
        cameras: The cameras attached.
    """

    name = "fake"

    def __init__(self, cameras: Iterable[FakeCamera] = ()) -> None:
        self.cameras = list(cameras)
        self.connections: list[FakeConnection] = []

    def discover(self) -> list[DeviceInfo]:
        return [
            DeviceInfo(self.name, c.model, serial_number=c.serial)
            for c in self.cameras
            if c.attached
        ]

    def open(self, serial: str) -> FakeConnection | None:
        camera = next(
            (c for c in self.cameras if c.serial == serial and c.attached), None
        )
        if camera is None:
            return None
        try:
            camera.open()
        except FakeCameraError as exc:
            raise _translated(exc) from None
        connection = FakeConnection(camera)
        self.connections.append(connection)
        return connection
