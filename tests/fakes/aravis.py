"""A fake ``gi.repository`` holding ``Aravis`` and ``GLib``, over `FakeCamera`s.

`FakeRepository(cameras)` stands in for ``gi.repository`` after
``gi.require_version("Aravis", "0.8")``. Pass it to the Aravis driver in place
of the real one. Names follow Aravis 0.8 through PyGObject
(``docs/hardware/usb3-vision.md``, Aravis Python API); behaviour that hasn't
been checked against real Aravis is marked there as inferred.

The stream follows Aravis's two queues. The driver pushes empty buffers into
the input queue; frames land in the output queue. A blocking pop
(``timeout_pop_buffer`` or ``pop_buffer``) lets one frame arrive if the camera
would send one, standing in for time passing. ``try_pop_buffer`` takes only
frames that have already arrived: in trigger mode, those the triggers asked
for; tests can make more arrive with `FakeArvStream.arrive()`. A frame needs a
free input buffer, so once the driver holds every buffer, pops time out.

Like real Aravis, nothing is closed explicitly: a camera lets go of its
hardware when its last reference (camera, device, or stream) is dropped.
"""

import enum
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from fakes.genicam import PFNC, FakeCamera, FakeCameraError, FakeFrame

__all__ = ["PFNC", "FakeArvBuffer", "FakeArvCamera", "FakeArvStream", "FakeRepository"]


class GLibError(Exception):
    """Stands in for ``gi.repository.GLib.Error``."""

    def __init__(self, message: str, domain: str, code: int) -> None:
        super().__init__(message)
        self.message = message
        self.domain = domain
        self.code = code

    def __str__(self) -> str:
        return f"{self.domain}: {self.message} ({self.code})"


class _GLib:
    Error = GLibError


class DeviceError(enum.IntEnum):
    WRONG_FEATURE = 0
    FEATURE_NOT_FOUND = 1
    NOT_CONNECTED = 2
    PROTOCOL_ERROR = 3
    TRANSFER_ERROR = 4
    TIMEOUT = 5
    NOT_FOUND = 6
    INVALID_PARAMETER = 7
    GENICAM_NOT_FOUND = 8
    NO_STREAM_CHANNEL = 9
    NOT_CONTROLLER = 10
    UNKNOWN = 11


class GcError(enum.IntEnum):
    PROPERTY_NOT_DEFINED = 0
    PVALUE_NOT_DEFINED = 1
    INVALID_PVALUE = 2
    EMPTY_ENUMERATION = 3
    OUT_OF_RANGE = 4
    NO_DEVICE_SET = 5
    NO_EVENT_IMPLEMENTATION = 6
    NODE_NOT_FOUND = 7
    ENUM_ENTRY_NOT_FOUND = 8
    INVALID_LENGTH = 9
    READ_ONLY = 10


class BufferStatus(enum.IntEnum):
    UNKNOWN = -1
    SUCCESS = 0
    CLEARED = 1
    TIMEOUT = 2
    MISSING_PACKETS = 3
    WRONG_PACKET_ID = 4
    SIZE_MISMATCH = 5
    FILLING = 6
    ABORTED = 7
    PAYLOAD_NOT_SUPPORTED = 8

    @property
    def value_nick(self) -> str:
        return self.name.lower().replace("_", "-")


class UvUsbMode(enum.IntEnum):
    SYNC = 0
    ASYNC = 1


class StreamCallbackType(enum.IntEnum):
    INIT = 0
    EXIT = 1
    START_BUFFER = 2
    BUFFER_DONE = 3


class GcAccessMode(enum.IntEnum):
    UNDEFINED = -1
    RO = 0
    WO = 1
    RW = 2


class RangeCheckPolicy(enum.IntEnum):
    DISABLE = 0
    ENABLE = 1
    DEBUG = 2


class AccessCheckPolicy(enum.IntEnum):
    DISABLE = 0
    ENABLE = 1


DEVICE_DOMAIN = "arv-device-error-quark"
GC_DOMAIN = "arv-gc-error-quark"

# How each FakeCamera failure surfaces through Aravis (inferred; hardware doc).
_ERRORS = {
    "not_found": (GC_DOMAIN, GcError.NODE_NOT_FOUND),
    "access": (GC_DOMAIN, GcError.READ_ONLY),
    "range": (GC_DOMAIN, GcError.OUT_OF_RANGE),
    "timeout": (DEVICE_DOMAIN, DeviceError.TIMEOUT),
    "state": (DEVICE_DOMAIN, DeviceError.PROTOCOL_ERROR),
    "busy": (DEVICE_DOMAIN, DeviceError.NOT_CONNECTED),
}


def _raise(exc: FakeCameraError) -> GLibError:
    domain, code = _ERRORS[exc.kind]
    return GLibError(str(exc), domain, int(code))


def _translated(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except FakeCameraError as exc:
        error = _raise(exc)
    raise error


class FakeArvBuffer:
    """An ``Aravis.Buffer``: empty until the stream fills it with a frame."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.frame: FakeFrame | None = None
        self.status = BufferStatus.CLEARED

    def _filled(self) -> FakeFrame:
        if self.frame is None:
            raise AssertionError("the driver read a buffer that holds no frame")
        return self.frame

    def get_status(self) -> BufferStatus:
        return self.status

    def get_data(self) -> bytes:
        return self._filled().pixels.tobytes()

    def get_image_width(self) -> int:
        return self._filled().pixels.shape[1]

    def get_image_height(self) -> int:
        return self._filled().pixels.shape[0]

    def get_image_pixel_format(self) -> int:
        return PFNC[self._filled().pixel_format]

    def get_frame_id(self) -> int:
        return self._filled().frame_id

    def get_timestamp(self) -> int:
        return self._filled().timestamp_ns

    def get_system_timestamp(self) -> int:
        return self._filled().camera.device_time_ns()


class FakeArvStream:
    """An ``Aravis.Stream``, with its input and output queues."""

    def __init__(
        self, camera: "FakeArvCamera", callback: Any = None, user_data: Any = None
    ) -> None:
        self.camera = camera
        self.callback = callback
        self.user_data = user_data
        # Creating a stream enables the camera's stream channel; finalizing one
        # disables it, even if a newer stream has enabled it since (assumed from
        # arv_uv_stream_finalize; usb3-vision.md).
        camera.channel_enabled = True
        self.input: deque[FakeArvBuffer] = deque()
        self.output: deque[FakeArvBuffer] = deque()
        self.pops: list[tuple[str, int | None]] = []

    @property
    def sim(self) -> FakeCamera:
        return self.camera.sim

    def push_buffer(self, buffer: FakeArvBuffer) -> None:
        if buffer.frame is not None:
            buffer.frame.release()
            buffer.frame = None
        buffer.status = BufferStatus.CLEARED
        self.input.append(buffer)

    def arrive(self, count: int = 1) -> int:
        """Let up to ``count`` frames land in the output queue; returns how many."""
        landed = 0
        while landed < count and self._can_arrive():
            buffer = self.input.popleft()
            if self.camera.payload_for_format() > buffer.size:
                buffer.status = BufferStatus.SIZE_MISMATCH
                buffer.frame = None
                self._land(buffer)
                landed += 1
                continue
            frame = self.sim.next_frame()
            buffer.frame = frame
            buffer.status = (
                BufferStatus.SUCCESS
                if frame.status.name == "COMPLETE"
                else BufferStatus.MISSING_PACKETS
            )
            self._land(buffer)
            landed += 1
        return landed

    def _land(self, buffer: FakeArvBuffer) -> None:
        # As in arvuvstream.c: queue the buffer, then tell the callback.
        self.output.append(buffer)
        if self.callback is not None:
            self.callback(self.user_data, StreamCallbackType.BUFFER_DONE, buffer)

    def _can_arrive(self) -> bool:
        return (
            bool(self.input)
            and self.camera.acquiring
            and self.camera.channel_enabled
            and self.sim.frame_waiting()
        )

    def __del__(self) -> None:
        camera = self.__dict__.get("camera")
        if camera is not None:
            camera.channel_enabled = False
        # Finalizing a stream frees the buffers still in its queues.
        for buffer in [
            *self.__dict__.get("output", ()),
            *self.__dict__.get("input", ()),
        ]:
            if buffer.frame is not None and not buffer.frame.released:
                buffer.frame.release()
                buffer.frame = None

    def try_pop_buffer(self) -> FakeArvBuffer | None:
        self.pops.append(("try", None))
        if self.sim.triggered:
            self.arrive(len(self.sim.pending_triggers) + len(self.sim.queue))
        return self.output.popleft() if self.output else None

    def timeout_pop_buffer(self, timeout_us: int) -> FakeArvBuffer | None:
        self.pops.append(("timeout", timeout_us))
        if not self.output:
            self.arrive()
        return self.output.popleft() if self.output else None

    def pop_buffer(self) -> FakeArvBuffer | None:
        self.pops.append(("blocking", None))
        if not self.output:
            self.arrive()
        if not self.output:
            # Real Aravis would block forever; failing keeps tests from hanging.
            raise AssertionError("pop_buffer() would block forever")
        return self.output.popleft()

    def get_n_buffers(self) -> tuple[int, int]:
        return len(self.input), len(self.output)


class FakeArvDevice:
    """An ``Aravis.Device``: generic GenICam feature access."""

    def __init__(self, sim: FakeCamera, channel: Callable[[], bool]) -> None:
        # Only the simulated camera and a function, not the FakeArvCamera: a
        # reference cycle would keep the camera open after the driver drops it.
        self.sim = sim
        self.camera_channel_enabled = channel
        self.range_check = RangeCheckPolicy.DISABLE
        self.access_check = AccessCheckPolicy.DISABLE

    def set_range_check_policy(self, policy: RangeCheckPolicy) -> None:
        self.range_check = policy

    def set_access_check_policy(self, policy: AccessCheckPolicy) -> None:
        self.access_check = policy

    def get_feature(self, name: str) -> "FakeArvFeature | None":
        return FakeArvFeature(self.sim, name) if name in self.sim.nodes else None

    def is_feature_available(self, name: str) -> bool:
        if name not in self.sim.nodes:
            raise GLibError(f"no feature {name}", GC_DOMAIN, GcError.NODE_NOT_FOUND)
        return True

    def get_feature_access_mode(self, name: str) -> GcAccessMode:
        # Like Aravis, this ignores locks: a node whose access changes with other
        # settings reads as RW, and is_locked() says whether it is locked now.
        node = self.sim.nodes[name]
        if callable(node.access):
            return GcAccessMode.RW
        return (
            GcAccessMode.UNDEFINED if node.access == "NA" else GcAccessMode[node.access]
        )

    def _read(self, name: str) -> Any:
        return _translated(lambda: self.sim.read(name))

    def _write(self, name: str, value: Any) -> None:
        if name == "PixelFormat" and self.camera_channel_enabled():
            # The U3-36PxXCP-M refuses payload settings while a stream channel is
            # enabled, even with acquisition stopped (verified).
            raise GLibError(
                "[PixelFormat] USB3Vision write_memory error (error)",
                DEVICE_DOMAIN,
                int(DeviceError.PROTOCOL_ERROR),
            )
        _translated(lambda: self.sim.write(name, value))

    def get_float_feature_value(self, name: str) -> float:
        return float(self._read(name))

    def set_float_feature_value(self, name: str, value: float) -> None:
        self._write(name, float(value))

    def get_float_feature_bounds(self, name: str) -> tuple[float, float]:
        low, high = _translated(lambda: self.sim.range(name))
        return float(low), float(high)

    def get_integer_feature_value(self, name: str) -> int:
        return int(self._read(name))

    def set_integer_feature_value(self, name: str, value: int) -> None:
        self._write(name, int(value))

    def get_integer_feature_bounds(self, name: str) -> tuple[int, int]:
        low, high, _ = _translated(lambda: self.sim.range(name))
        return int(low), int(high)

    def get_integer_feature_increment(self, name: str) -> int:
        _, _, inc = _translated(lambda: self.sim.range(name))
        return int(inc)

    def get_string_feature_value(self, name: str) -> str:
        return str(self._read(name))

    def set_string_feature_value(self, name: str, value: str) -> None:
        self._write(name, value)

    def dup_available_enumeration_feature_values_as_strings(
        self, name: str
    ) -> list[str]:
        return list(_translated(lambda: self.sim.entries(name)))

    def execute_command(self, name: str) -> None:
        _translated(lambda: self.sim.execute(name))


class FakeArvFeature:
    """An ``Aravis.GcFeatureNode``, for its lock state."""

    def __init__(self, sim: FakeCamera, name: str) -> None:
        self._sim = sim
        self._name = name

    def is_locked(self) -> bool:
        node = self._sim.nodes[self._name]
        return callable(node.access) and self._sim.access(self._name) == "RO"


class FakeArvCamera:
    """An ``Aravis.Camera``. Its hardware is let go when the last reference to it,
    its device, or a stream goes."""

    def __init__(self, sim: FakeCamera) -> None:
        _translated(sim.open)
        self.sim = sim
        self.acquiring = False
        self.usb_mode = UvUsbMode.ASYNC
        channel = [False]
        self._channel = channel
        self.device = FakeArvDevice(sim, lambda: channel[0])
        self.streams_created = 0

    def __del__(self) -> None:
        sim = self.__dict__.get("sim")
        if sim is None:
            return
        sim.streaming = False
        sim.opened = False

    @property
    def channel_enabled(self) -> bool:
        return self._channel[0]

    @channel_enabled.setter
    def channel_enabled(self, value: bool) -> None:
        self._channel[0] = value

    def is_uv_device(self) -> bool:
        return True

    def uv_set_usb_mode(self, mode: UvUsbMode) -> None:
        self.usb_mode = mode

    def get_device(self) -> FakeArvDevice:
        return self.device

    def get_model_name(self) -> str:
        return self.sim.model

    def get_device_serial_number(self) -> str:
        return self.sim.serial

    def payload_for_format(self) -> int:
        fmt = self.sim.get("PixelFormat")
        bytes_per_pixel = {"Mono16": 2, "BayerRG16": 2, "RGB8": 3}.get(fmt, 1)
        return self.sim.width * self.sim.height * bytes_per_pixel

    def get_payload(self) -> int:
        return self.payload_for_format()

    def create_stream(
        self, callback: Any = None, user_data: Any = None
    ) -> FakeArvStream:
        self.streams_created += 1
        return FakeArvStream(self, callback, user_data)

    def start_acquisition(self) -> None:
        _translated(self.sim.start)
        self.acquiring = True

    def stop_acquisition(self) -> None:
        _translated(self.sim.stop)
        self.acquiring = False


class _Aravis:
    """The ``Aravis`` namespace, bound to one repository's cameras."""

    DeviceError = DeviceError
    GcError = GcError
    BufferStatus = BufferStatus
    GcAccessMode = GcAccessMode
    StreamCallbackType = StreamCallbackType
    UvUsbMode = UvUsbMode
    RangeCheckPolicy = RangeCheckPolicy
    AccessCheckPolicy = AccessCheckPolicy
    Stream = FakeArvStream
    Device = FakeArvDevice

    def __init__(self, repository: "FakeRepository") -> None:
        self._repository = repository
        self._listed: list[FakeCamera] = []

        class Camera(FakeArvCamera):
            @classmethod
            def new(cls, name: str | None) -> FakeArvCamera:
                return repository._open(name)

        class Buffer(FakeArvBuffer):
            @classmethod
            def new_allocate(cls, size: int) -> FakeArvBuffer:
                repository.buffers_allocated += 1
                return FakeArvBuffer(size)

        self.Camera = Camera
        self.Buffer = Buffer

    def update_device_list(self) -> None:
        self._repository.update_calls += 1
        self._listed = [c for c in self._repository.cameras if c.attached]

    def get_n_devices(self) -> int:
        return len(self._listed)

    def get_device_id(self, index: int) -> str:
        cam = self._listed[index]
        return f"{cam.vendor.split()[0]}-{cam.serial}"

    def get_device_serial_nbr(self, index: int) -> str:
        return self._listed[index].serial

    def get_device_model(self, index: int) -> str:
        return self._listed[index].model

    def get_device_vendor(self, index: int) -> str:
        return self._listed[index].vendor

    def get_device_physical_id(self, index: int) -> str:
        return f"usb:1-{index + 1}"

    def get_device_protocol(self, index: int) -> str:
        return "USB3Vision"


class FakeRepository:
    """Stands in for ``gi.repository``, with ``Aravis`` and ``GLib`` attributes.

    Args:
        cameras: The cameras attached.
    """

    def __init__(self, cameras: Iterable[FakeCamera] = ()) -> None:
        self.cameras = list(cameras)
        self.update_calls = 0
        self.open_attempts = 0
        self.buffers_allocated = 0
        self.GLib = _GLib
        self.Aravis = _Aravis(self)

    def _open(self, name: str | None) -> FakeArvCamera:
        self.open_attempts += 1
        for cam in self.cameras:
            if cam.attached and f"{cam.vendor.split()[0]}-{cam.serial}" == name:
                return FakeArvCamera(cam)
        raise GLibError(
            f"camera {name} not found", DEVICE_DOMAIN, DeviceError.NOT_FOUND
        )
