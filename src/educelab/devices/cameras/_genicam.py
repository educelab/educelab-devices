"""The core shared by every GenICam camera driver, and its backend seam.

`GenICamCamera` holds the SFNC behaviour that any GenICam camera shares
(ADR 0006): settings and their units, pausing acquisition for writes a camera
only takes while stopped, software triggering, the device-to-host timestamp
offset, and the ``not_before`` loop. It talks to the camera only through a
`Backend` and the `Connection` it opens. Each library (PySpin, Aravis, GenTL)
has one backend, and tests swap in a fake. See ``docs/hardware/spinnaker.md``
for the behaviour this was first built and verified against.

Backends translate their library's errors into `DeviceError` subclasses: a
grab or other wait that runs out raises `DeviceTimeout`, a camera held by
another process raises `DeviceBusy`, and anything else raises `DeviceError`
with the library's own message. The core adds which camera and what it was
doing.
"""

import enum
import functools
import logging
import math
import statistics
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TypeVar

import numpy as np
import numpy.typing as npt

from educelab.devices.cameras._preview import (
    HostCrop,
    hardware_roi,
    host_crop,
    pick_decimation,
)
from educelab.devices.cameras.base import (
    EXPOSURE_TIME,
    GAIN,
    HEIGHT,
    PIXEL_FORMAT,
    SENSOR_HEIGHT,
    SENSOR_WIDTH,
    WIDTH,
    Frame,
    FrameCamera,
    PreviewFrame,
    PreviewGeometry,
    PreviewRegion,
    Setting,
    SettingValue,
)
from educelab.devices.device import DeviceInfo, synchronized
from educelab.devices.errors import (
    CommandRejected,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    NotConnected,
    StreamStalled,
)

__all__ = [
    "ARRIVAL_SAMPLES",
    "RETRIES_BEFORE_REMEASURE",
    "THROUGHPUT_LIMIT",
    "TIMESTAMP_SAMPLES",
    "TRIGGER",
    "Backend",
    "Buffer",
    "BufferStatus",
    "Connection",
    "GenICamCamera",
    "host_time",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BufferStatus(enum.Enum):
    """Whether a buffer holds a whole frame."""

    COMPLETE = "complete"
    MISSING_TRAILER = "missing trailer"
    """All the image data arrived, but not the packet that closes it."""
    INCOMPLETE = "incomplete"


class Buffer(Protocol):
    """One frame's buffer, held until `release()`.

    Reading a released buffer is undefined; copy the pixels first.
    """

    def status(self) -> BufferStatus: ...

    def status_text(self) -> str:
        """The library's description of the status, for logs."""
        ...

    def device_timestamp_ns(self) -> int:
        """The camera's timestamp counter for this frame, in nanoseconds."""
        ...

    def frame_id(self) -> int: ...

    def host_timestamp_ns(self) -> int | None:
        """Host time, in nanoseconds since the epoch, when the frame began to
        arrive, or None if the library doesn't say."""
        ...

    def pixel_format(self) -> str:
        """The SFNC name of the pixel format, e.g. ``"Mono8"``."""
        ...

    def pixels(self) -> npt.NDArray[Any]:
        """A copy of the image, shaped ``(height, width)`` or ``(height, width, 3)``."""
        ...

    def release(self) -> None:
        """Give the buffer back to the stream."""
        ...


class Connection(Protocol):
    """One open camera: its node map and its stream.

    Nodes are named as in SFNC (e.g. ``"ExposureTime"``) and keep their native
    units. A node the camera lacks is neither readable nor writable, and
    reading or writing it raises `DeviceError`.

    The stream keeps only the newest frame: a grab returns the most recent
    one and drops any older ones still waiting.
    """

    times_arrivals: bool
    """Whether this library's buffers carry host arrival times
    (`Buffer.host_timestamp_ns`)."""

    drains_on_stop: bool
    """Whether stopping waits for the camera to finish sending, and so needs the
    exposure time. The core reads it only for a connection that does."""

    def has(self, name: str) -> bool:
        """Whether the camera has the node at all, whatever its access."""
        ...

    def is_readable(self, name: str) -> bool: ...

    def is_writable(self, name: str) -> bool: ...

    def get_float(self, name: str) -> float: ...

    def get_int(self, name: str) -> int: ...

    def get_string(self, name: str) -> str: ...

    def get_enum(self, name: str) -> str:
        """The symbolic name of the current entry."""
        ...

    def float_range(self, name: str) -> tuple[float, float]:
        """The node's ``(minimum, maximum)``."""
        ...

    def int_range(self, name: str) -> tuple[int, int, int]:
        """The node's ``(minimum, maximum, increment)``."""
        ...

    def enum_entries(self, name: str) -> tuple[str, ...]:
        """The symbolic names of the entries currently available."""
        ...

    def set_float(self, name: str, value: float) -> None: ...

    def set_int(self, name: str, value: int) -> None: ...

    def set_enum(self, name: str, symbol: str) -> None: ...

    def execute(self, name: str) -> None:
        """Run a command node, e.g. ``"TriggerSoftware"``."""
        ...

    def start_acquisition(self, exposure_time: float = 0.0) -> None:
        """Start acquiring.

        ``exposure_time`` is as for `stop_acquisition`, for undoing a start
        that fails.
        """
        ...

    def stop_acquisition(self, exposure_time: float = 0.0) -> None:
        """Stop acquiring. Does nothing if already stopped.

        ``exposure_time`` is the camera's exposure in seconds: a frame being
        exposed at the stop is sent once it ends, which a backend that waits
        for the camera to finish sending must allow for.
        """
        ...

    def grab(self, timeout: float | None) -> Buffer:
        """The newest frame, waiting up to ``timeout`` seconds for one.

        ``None`` waits indefinitely, and ``0`` takes a frame only if one is
        already waiting.

        Raises:
            DeviceTimeout: No frame came in time.
        """
        ...

    def stream_counters(self) -> Mapping[str, int]:
        """The library's counts for the stream (e.g. ``frames_delivered``), for
        diagnosis; empty where the backend has none. Never raises: a counter
        that can't be read is left out."""
        ...

    def close(self, exposure_time: float = 0.0) -> None:
        """Stop acquiring (see `stop_acquisition`) and let go of the camera.
        Does nothing if closed.

        Everything is released even if a step fails.

        Raises:
            DeviceError: A step failed; the message lists each failure.
        """
        ...


class Backend(Protocol):
    """One library's way of finding and opening GenICam cameras."""

    name: str
    """The short name reported in `DeviceInfo.backend`, e.g. ``"aravis"``."""

    def discover(self) -> list[DeviceInfo]:
        """List attached cameras without opening them.

        Raises:
            ImportError: The library isn't installed.
            DeviceError: The library couldn't enumerate cameras.
        """
        ...

    def open(self, serial: str) -> Connection | None:
        """Open the camera with this serial number, or return None if none is
        attached.

        Raises:
            ImportError: The library isn't installed.
            DeviceBusy: Another process holds the camera.
            DeviceError: The camera was found but couldn't be opened.
        """
        ...


# --- The core -----------------------------------------------------------------

# Latch samples whose median gives the device-to-host timestamp offset; the
# count comes from pgs-scan (and multi_pyspin before it). See spinnaker.md.
TIMESTAMP_SAMPLES = 20
# Newer cameras name the latched value "Timestamp", older ones
# "TimestampLatchValue" (docs/hardware/spinnaker.md, Protocol).
_TIMESTAMP_NODES = ("Timestamp", "TimestampLatchValue")

# Exposure time, in microseconds: SFNC's name, then the older one that some
# cameras (and Aravis's simulated camera) still use.
_EXPOSURE_NODES = ("ExposureTime", "ExposureTimeAbs")

# Recent frames whose arrival times estimate the offset of a camera that can't
# latch its timestamp; the smallest gap has the least transfer delay in it.
ARRIVAL_SAMPLES = 20

# Stale frames a capture drops before re-measuring the timestamp offset, as in
# pgs-scan dev. The capture's timeout bounds the retries. See spinnaker.md.
RETRIES_BEFORE_REMEASURE = 5

# Longest side of a preview, in pixels; larger frames are subsampled.

# Pixel formats whose arrays the driver knows how to shape. Others (e.g.
# packed 12-bit) would come back as raw bytes.
_PIXEL_FORMATS = frozenset(
    {"Mono8", "Mono16", "RGB8", "Mono10g40IDS"}
    | {f"Bayer{order}{bits}" for order in ("RG", "GB", "GR", "BG") for bits in (8, 16)}
)
# PFNC codes of those formats, for libraries that report a buffer's format as a
# number (Aravis, GenTL). Values from the PFNC, as in Aravis's arvenums.h.
_PFNC = {
    0x01080001: "Mono8",
    0x01100007: "Mono16",
    0x02180014: "RGB8",
    0x01080008: "BayerGR8",
    0x01080009: "BayerRG8",
    0x0108000A: "BayerGB8",
    0x0108000B: "BayerBG8",
    0x0110002E: "BayerGR16",
    0x0110002F: "BayerRG16",
    0x01100030: "BayerGB16",
    0x01100031: "BayerBG16",
    # IDS's own code, outside the PFNC (verified on the U3-36PxXCP-M).
    0x4000000F: "Mono10g40IDS",
}
_IDS = "IDS Imaging Development Systems GmbH"
# Vendors whose Gain node is a linear factor (1 = unity), not SFNC's dB: IDS's
# node describes it as "an amplification factor" (ids-u3-36p0xcp.md, Nodes).
_LINEAR_GAIN_VENDORS = frozenset({_IDS})
# Such a camera keeps a factor to this many binary places and rounds others
# down: steps of 1/16 from 1 to 2, 1/8 from 2 to 4, and so on.
_GAIN_MANTISSA_BITS = 4
# Vendors whose cameras lower AcquisitionFrameRate to fit a longer exposure and
# leave it there. FLIR's work the other way: the frame rate caps the exposure.
_SELF_SLOWING_VENDORS = frozenset({_IDS})
# "Once" isn't offered: it reverts to "Off" by itself, so it never reads back.
_AUTO_MODES = ("Off", "Continuous")
# A geometry change re-rounds the exposure a little; less than this is a cut.
_EXPOSURE_KEPT = 0.99
_US_PER_S = 1e6

THROUGHPUT_LIMIT = "throughput_limit"
"""The cap on the camera's USB bandwidth, in MB/s, where the camera has one."""
# MB here is 10^6 bytes. DeviceLinkThroughputLimit counts bytes per second
# (verified on the CM3-U3-50S5M; see spinnaker.md).
_BYTES_PER_MB = 1e6

TRIGGER = "trigger"
"""``"Off"`` to free-run, ``"Software"`` to expose a frame only when a capture
asks for one. Only on cameras with a software trigger."""
_TRIGGER_MODES = ("Off", "Software")
# How far before a software trigger a frame's exposure may seem to start and still
# count as triggered by it: room for error in the timestamp offset.
_TRIGGER_TOLERANCE_S = 0.001
# A camera ignores triggers for a while after acquisition starts, so the driver
# fires until one is answered, waiting the exposure plus this long for each.
_PRIME_WAIT_S = 1.0
# A capture fires again if no frame comes within the exposure plus this many
# times the answered trigger's turnaround (docs/hardware/spinnaker.md).
_RETRIGGER_TURNAROUNDS = 3
# More than any stream's buffer pool, so discarding waiting frames always ends.
_MAX_DISCARDED = 100
# A software trigger waits this long after the last frame: the IDS U3-36PxXCP
# ignores triggers for about 3 ms after one (ids-u3-36p0xcp.md, Software trigger).
_TRIGGER_SETTLE_S = 0.01
# Recent software triggers kept for dating frames by trigger; more than a
# frame can be in flight across.
_FIRES_KEPT = 16
# A wall clock coarser than this is too coarse for timestamp offsets: before
# Python 3.13, Windows' ticks every 15.6 ms (usb3-vision.md, Hardware runs).
_COARSE_CLOCK_S = 0.001
# How long to wait for a coarse wall clock to tick, and how often to line the
# fine counter up with it again, so the two don't drift apart.
_ANCHOR_WAIT_S = 0.05
_REANCHOR_S = 60.0


class _HostClock:
    """The host's wall-clock time in seconds, finer than ``time.time()`` where
    that is coarse.

    There, it counts ``time.perf_counter()`` from a moment the wall clock
    ticked. If the wall clock never ticks while it waits, it falls back to it.
    """

    def __init__(
        self,
        *,
        coarse: bool | None = None,
        wall: Callable[[], float] | None = None,
        fine: Callable[[], float] | None = None,
    ) -> None:
        # Looked up at each call, so tests can patch time.time.
        self._wall = wall or (lambda: time.time())
        self._fine = fine or time.perf_counter
        if coarse is None:
            coarse = time.get_clock_info("time").resolution > _COARSE_CLOCK_S
        self._coarse = coarse
        self._base: float | None = None
        self._anchored_at = -math.inf

    def now(self) -> float:
        if not self._coarse:
            return self._wall()
        if self._fine() - self._anchored_at > _REANCHOR_S:
            self.anchor()
        if self._base is None:
            return self._wall()
        return self._base + self._fine()

    def anchor(self) -> None:
        """Line the fine counter up with the wall clock at one of its ticks."""
        if not self._coarse:
            return
        start = self._wall()
        deadline = self._fine() + _ANCHOR_WAIT_S
        while (fine := self._fine()) < deadline:
            wall = self._wall()
            if wall != start:
                self._base = wall - fine
                break
        self._anchored_at = self._fine()


_HOST_CLOCK = _HostClock()


def _counters_text(counters: Mapping[str, int]) -> str:
    return ", ".join(f"{name} {value}" for name, value in counters.items()) or "none"


def host_time() -> float:
    """The host's wall-clock time in seconds, as ``time.time()`` but fine-grained
    on every platform. Frame timestamps are measured against it."""
    return _HOST_CLOCK.now()


class GenICamCamera(FrameCamera):
    """One GenICam camera, addressed by serial number, through a backend.

    ``open()`` opens the camera, measures the offset between its timestamp
    counter and the host clock, and starts free-running acquisition that
    keeps only the newest frame, which is the fastest way to capture.
    ``close()`` stops acquiring and lets go of the camera. Changing
    ``pixel_format`` or ``trigger`` pauses acquisition briefly, since cameras
    only accept them while stopped. With ``trigger`` set to ``"Software"``,
    each capture fires a trigger (or collects one fired by `trigger()`) and
    returns that frame, so nothing streams between captures.

    A frame's ``timestamp`` is the start of its exposure, in host time. The
    raw device counter, ``device_timestamp_ns`` in its metadata, marks the end.

    Once open, ``info.serial_number`` is the camera's own ``DeviceSerialNumber``,
    which can differ from the serial it was opened by: Aravis lists FLIR cameras
    by their USB serial, for example.

    Settings for features the camera lacks (e.g. a software trigger or a
    throughput limit) are left out.

    Args:
        serial: The camera's serial number, as its backend's discovery reports it.
        backend: The library that reaches the camera.
    """

    label = "GenICam"
    """Names the kind of camera in messages, e.g. "no GenICam camera with ..."."""

    # Added to a grab timeout on a camera that hasn't sent a frame since open.
    _no_frame_hint = "another program may be streaming from this camera"

    def __init__(self, serial: str, backend: Backend) -> None:
        super().__init__()
        self._serial = str(serial)
        self._backend = backend
        self._conn: Connection | None = None
        self._model = f"{self.label} camera"
        self._vendor: str | None = None
        self._firmware: str | None = None
        self._own_serial: str | None = None
        self._timestamp_node: str | None = None
        self._exposure_node: str | None = None
        self._self_slowing = False
        self._descriptors: dict[str, _Descriptor] = {}
        self._timestamp_offset: float | None = None
        self._timestamp_source: str | None = None
        self._arrival_gaps: deque[float] = deque(maxlen=ARRIVAL_SAMPLES)
        self._delivered = False
        self._software_trigger = False
        self._fired_at: float | None = None
        self._fires: deque[float] = deque(maxlen=_FIRES_KEPT)
        self._last_frame_at = -math.inf
        self._trigger_turnaround: float | None = None
        self._crop: HostCrop | None = None
        # Set when the camera takes an ROI: the geometry captures use.
        self._capture_geometry: _Geometry | None = None
        self._decimations: tuple[int, ...] = (1,)
        self._limits: dict[int, _GeometryLimits] = {}
        # What preview last wrote, or None while the capture geometry stands.
        self._applied: _Geometry | None = None
        self._limit_before_preview: int | None = None
        # The longest exposure seen at each ROI size, in the node's unit.
        self._exposure_caps: dict[tuple[int, int, int], float] = {}
        # Frames from before this show the previous preview geometry.
        self._preview_floor: datetime | None = None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._serial}>"

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            self._backend.name,
            self._model,
            serial_number=self._own_serial or self._serial,
            firmware_version=self._firmware,
            vendor=self._vendor,
        )

    @property
    def timestamp_node(self) -> str | None:
        """The node holding the latched timestamp: ``"Timestamp"`` or the older
        ``"TimestampLatchValue"``. ``None`` before open, or if the camera has neither.
        """
        return self._timestamp_node

    @property
    def timestamp_offset(self) -> float | None:
        """Host clock minus the camera's timestamp counter, in seconds.

        ``None`` if the camera can't latch its timestamp.
        """
        return self._timestamp_offset

    @property
    def timestamp_source(self) -> str | None:
        """How `timestamp_offset` is measured: ``"latch"`` from the camera's
        timestamp latch, ``"arrival"`` from when frames reach the host (for
        cameras without a latch; about a millisecond less exact), or ``None``
        if it can't be.

        A frame's ``metadata["timestamp_source"]`` is ``"trigger"`` instead when
        it was dated by the software trigger that exposed it, because the
        camera stamped it before that trigger.
        """
        return self._timestamp_source

    @synchronized
    def trigger(self) -> None:
        """Fire a software trigger now, for the next capture to collect.

        Only in ``trigger`` mode ``"Software"``. Lets a caller start several
        cameras' exposures together before collecting their frames; without it,
        each capture fires its own trigger.
        """
        if not self._software_trigger:
            raise DeviceError(f"{self!r} isn't in software-trigger mode")
        self._discard_waiting_frames()
        self._fired_at = self._fire()

    @synchronized
    def measure_timestamp_offset(self) -> float | None:
        """Measure `timestamp_offset` again, e.g. after the host clock jumps.

        Returns:
            The new offset.
        """
        self._measure_timestamp_offset()
        return self._timestamp_offset

    @synchronized
    def diagnostics(self) -> dict[str, int]:
        """Counters for diagnosing a stream that stops, where the camera and
        backend have them.

        The camera's ``transmit_failures`` (``TransmitFailureCount``) and
        ``link_recoveries`` (``USB3LinkRecoveryCount``), and the backend's stream
        counters, e.g. GenTL's ``frames_delivered``. Keys differ between
        cameras and backends; a counter that can't be read is left out.
        """
        return self._diagnostics()

    @synchronized
    def restart_acquisition(self) -> None:
        """Stop and start acquisition: the first thing to try on a stalled stream.

        Logs the camera's `diagnostics` before and after. A capture afterwards
        shows whether frames are back; if not, close and reopen the camera.
        """
        before = self._diagnostics()
        with self._acquisition_paused():
            pass
        logger.warning(
            "%r: restarted acquisition; counters before: %s; after: %s",
            self,
            _counters_text(before),
            _counters_text(self._diagnostics()),
        )

    def _diagnostics(self) -> dict[str, int]:
        conn = self._connection
        try:
            found = dict(conn.stream_counters())
        except DeviceError as exc:
            # A backend that breaks the never-raises contract still gets a report.
            logger.debug("%r: no stream counters: %s", self, exc)
            found = {}
        for node, key in _CAMERA_COUNTERS:
            try:
                if conn.is_readable(node):
                    read = functools.partial(conn.get_int, node)
                    found[key] = self._call(f"read {node}", read)
            except DeviceError as exc:
                logger.debug("%r: no %s: %s", self, node, exc)
        return found

    # --- Lifecycle -------------------------------------------------------------

    @property
    def _connection(self) -> Connection:
        if self._conn is None:
            raise NotConnected(f"{self!r} is not open")
        return self._conn

    def _open(self) -> None:
        conn = self._call("open", lambda: self._backend.open(self._serial))
        if conn is None:
            raise DeviceNotFound(self._not_found_message())
        self._conn = conn
        try:
            self._model = self._read_string("DeviceModelName") or self._model
            self._vendor = self._read_optional_string("DeviceVendorName")
            self._firmware = self._read_string("DeviceFirmwareVersion")
            self._own_serial = self._read_string("DeviceSerialNumber")
            self._configure()
            self._warn_of_a_slow_link()
            self._delivered = False
            self._software_trigger = False
            self._fired_at = None
            exposure = self._drain_exposure()
            self._call("begin acquisition", lambda: conn.start_acquisition(exposure))
            self._measure_timestamp_offset()
        except BaseException:
            for problem in self._release():
                logger.warning("%r: while giving up on open: %s", self, problem)
            raise

    def _close(self) -> None:
        problems = self._release()
        if problems:
            raise DeviceError(f"{self!r} didn't close cleanly: {'; '.join(problems)}")

    def _release(self) -> list[str]:
        """Let go of the camera, collecting what went wrong."""
        problems = []
        exposure = self._drain_exposure()
        conn, self._conn = self._conn, None
        self._descriptors = {}
        if conn is not None:
            try:
                conn.close(exposure)
            except DeviceError as exc:
                problems.append(str(exc))
        self._timestamp_offset = None
        self._timestamp_source = None
        self._arrival_gaps.clear()
        self._crop = self._applied = self._capture_geometry = None
        self._limit_before_preview = self._preview_floor = None
        self._limits = {}
        self._exposure_caps = {}
        return problems

    def _not_found_message(self) -> str:
        found = [info.serial_number for info in self._backend.discover()]
        attached = ", ".join(s for s in found if s) or "none"
        return (
            f"no {self.label} camera with serial number {self._serial}; "
            f"attached: {attached}"
        )

    def _configure(self) -> None:
        # Free-running; the backend keeps only the newest frame. The fastest way
        # to capture (pgs-scan dev; docs/hardware/spinnaker.md, Acquisition).
        if self._connection.has("AcquisitionMode"):
            self._set_enum("AcquisitionMode", "Continuous")
        if self._connection.is_readable("TriggerMode"):
            # A camera left in trigger mode (e.g. by a vendor viewer) would never
            # send a frame.
            self._set_enum("TriggerMode", "Off")
        formats = self._enum_choices("PixelFormat", _PIXEL_FORMATS)
        if formats and self._read_enum("PixelFormat") not in formats:
            preferred = "RGB8" if "RGB8" in formats else formats[0]
            logger.info("%r: switching pixel format to %s", self, preferred)
            self._set_enum("PixelFormat", preferred)
        self._timestamp_node = next(
            (n for n in _TIMESTAMP_NODES if self._connection.has(n)), None
        )
        self._exposure_node = next(
            (n for n in _EXPOSURE_NODES if self._connection.has(n)), None
        )
        self._self_slowing = self._vendor in _SELF_SLOWING_VENDORS
        self._setup_geometry()
        self._descriptors = self._describe_settings()
        # Another program may have left the camera slowed down.
        self._raise_frame_rate()

    def _warn_of_a_slow_link(self) -> None:
        # The CM3 that stalled had come up at USB 2 before (gentl-stall track).
        node = "U3VCurrentSpeed"
        if not self._connection.is_readable(node):
            return
        try:
            speed = self._read_enum(node)
        except DeviceError as exc:
            logger.debug("%r: no %s: %s", self, node, exc)
            return
        if _USB_SPEEDS.get(speed, math.inf) < _USB_SPEEDS["SuperSpeed"]:
            logger.warning(
                "%r is linked at %s, slower than USB 3: replug it into a USB 3 "
                "port, or expect fewer frames",
                self,
                speed,
            )

    @contextmanager
    def _acquisition_paused(self) -> Iterator[None]:
        conn = self._connection
        exposure = self._drain_exposure()
        self._call("end acquisition", lambda: conn.stop_acquisition(exposure))
        try:
            yield
        finally:
            self._call("begin acquisition", lambda: conn.start_acquisition(exposure))
        if self._software_trigger:
            self._prime_trigger()

    def _drain_exposure(self) -> float:
        """The exposure time in seconds for a connection that drains on a stop
        (`Connection.drains_on_stop`); 0 for any other, or if it can't be read.
        """
        conn = self._conn
        if conn is None or not conn.drains_on_stop:
            return 0.0
        try:
            return self._exposure() or 0.0
        except Exception as exc:  # noqa: BLE001
            # Only a hint, so it never blocks a stop: an unplugged camera must
            # still close. The text only, so the traceback doesn't hold the camera.
            logger.debug("%r: no exposure time for the drain: %s", self, str(exc))
            return 0.0

    def _prime_trigger(self) -> None:
        """Fire until the camera answers, so a capture's trigger isn't ignored.

        Also times the answered trigger's turnaround, which sets how long a
        capture waits before firing again.
        """
        conn = self._connection
        exposure = self._exposure() or 0.0
        deadline = time.perf_counter() + self.capture_timeout
        fired = 0
        while (remaining := deadline - time.perf_counter()) > 0:
            started = time.perf_counter()
            self._fire()
            fired += 1
            wait = min(remaining, exposure + _PRIME_WAIT_S)
            try:
                buffer = self._call("get a frame", functools.partial(conn.grab, wait))
            except DeviceTimeout:
                continue
            self._last_frame_at = time.perf_counter()
            self._trigger_turnaround = time.perf_counter() - started
            self._call("release a frame", buffer.release)
            self._delivered = True
            logger.info(
                "%r answered software trigger %d, in %.3f s",
                self,
                fired,
                self._trigger_turnaround,
            )
            self._discard_waiting_frames()
            return
        raise DeviceTimeout(
            f"{self!r} answered none of {fired} software triggers in "
            f"{self.capture_timeout:g} s"
        )

    def _measure_timestamp_offset(self) -> None:
        conn = self._connection
        node = self._timestamp_node
        if node is not None and conn.is_writable("TimestampLatch"):
            _HOST_CLOCK.anchor()
            offsets = []
            for _ in range(TIMESTAMP_SAMPLES):
                before = host_time()
                self._call(
                    "latch the timestamp", lambda: conn.execute("TimestampLatch")
                )
                after = host_time()
                device = self._call("read the timestamp", lambda: conn.get_int(node))
                offsets.append((before + after) / 2 - device / 1e9)
            self._timestamp_offset = statistics.median(offsets)
            self._timestamp_source = "latch"
        else:
            self._measure_from_arrivals()
        logger.debug(
            "%r timestamp offset %s (%s)",
            self,
            self._timestamp_offset,
            self._timestamp_source,
        )

    def _measure_from_arrivals(self) -> None:
        """Estimate the offset from one free-running frame's arrival time."""
        self._arrival_gaps.clear()
        self._timestamp_offset = None
        self._timestamp_source = None
        if not self._connection.times_arrivals:
            logger.warning("%r can't latch its timestamp or time arrivals", self)
            return
        if self._software_trigger:
            self._discard_waiting_frames()
        deadline = time.perf_counter() + self.capture_timeout
        while (remaining := deadline - time.perf_counter()) > 0:
            if self._software_trigger:
                self._fire()
            buffer = self._next_buffer(remaining)
            try:
                status, arrival, device = self._call(
                    "read a frame's times", functools.partial(_times, buffer)
                )
            finally:
                self._call("release a frame", buffer.release)
            if arrival is None:
                logger.warning("%r can't latch its timestamp or time arrivals", self)
                return
            if status is not BufferStatus.INCOMPLETE:
                self._note_arrival(arrival, device)
                return
        raise DeviceTimeout(
            f"{self!r}: no complete frame to time in {self.capture_timeout:g} s"
        )

    def _note_arrival(self, arrival_ns: int, device_ns: int) -> None:
        self._arrival_gaps.append(arrival_ns / 1e9 - device_ns / 1e9)
        self._timestamp_offset = min(self._arrival_gaps)
        self._timestamp_source = "arrival"

    # --- Role hooks ------------------------------------------------------------

    def _settings(self) -> Mapping[str, Setting]:
        found = (d.read(self) for d in self._descriptors.values())
        return {s.name: s for s in found if s is not None}

    def _setting(self, name: str) -> Setting:
        descriptor = self._descriptors.get(name)
        found = descriptor.read(self) if descriptor is not None else None
        if found is None:
            raise KeyError(f"{self!r} has no setting {name!r}")
        return found

    def _set_setting(self, name: str, value: SettingValue) -> None:
        logger.debug("%r: %s = %r", self, name, value)
        self._descriptors[name].write(self, value)

    def _describe_settings(self) -> dict[str, "_Descriptor"]:
        """Each setting's descriptor, for this camera's nodes and vendor."""
        gain: _Descriptor = _Float(GAIN, "Gain", "dB")
        if self._vendor in _LINEAR_GAIN_VENDORS:
            steps = self._read_gain_steps()
            gain = _GainSteps(steps) if steps else _LinearGain()
        rate_nodes: tuple[str, ...] = ("AcquisitionResultingFrameRate",)
        if self._self_slowing:
            # The rate such a camera lowered itself to is the rate it runs at
            # (the IDS U3-36PxXCP has only AcquisitionFrameRate).
            rate_nodes += ("AcquisitionFrameRate",)
        found: list[_Descriptor | None] = [
            None
            if self._exposure_node is None
            else _Float(
                EXPOSURE_TIME, self._exposure_node, "s", _US_PER_S, moves_rate=True
            ),
            _Enum("ExposureAuto", "ExposureAuto", _AUTO_MODES),
            gain,
            _Enum("GainAuto", "GainAuto", _AUTO_MODES),
            _Enum(PIXEL_FORMAT, "PixelFormat", _PIXEL_FORMATS, paused=True),
            # Read-only even where the camera could crop: only preview sets an ROI.
            _FrameSize(WIDTH, "Width"),
            _FrameSize(HEIGHT, "Height"),
            _ReadOnly(SENSOR_WIDTH, ("SensorWidth",), "px", integer=True),
            _ReadOnly(SENSOR_HEIGHT, ("SensorHeight",), "px", integer=True),
            _ThroughputLimit(),
            _Trigger(),
            _ReadOnly(
                "current_throughput",
                ("DeviceLinkCurrentThroughput",),
                "MB/s",
                _BYTES_PER_MB,
                integer=True,
            ),
            _ReadOnly("resulting_frame_rate", rate_nodes, "Hz"),
            _LinkSpeed(),
        ]
        return {d.name: d for d in found if d is not None}

    def _exposure(self) -> float | None:
        """The exposure time in seconds, or None if the camera has no such node."""
        exposure = self._descriptors.get(EXPOSURE_TIME)
        return None if exposure is None else exposure.current(self)

    def _gain(self) -> float | None:
        """The gain in dB, or None if the camera has no ``Gain`` node."""
        gain = self._descriptors.get(GAIN)
        return None if gain is None else gain.current(self)

    def _read_gain_steps(self) -> dict[float, float]:
        """On a linear-gain camera, the dB of each factor it keeps, to that factor."""
        conn = self._connection
        if not conn.is_readable("Gain"):
            return {}
        low, high = self._call(
            "read the limits of Gain", lambda: conn.float_range("Gain")
        )
        factors = _binary_steps(low, high, _GAIN_MANTISSA_BITS)
        if len(factors) < 2:
            logger.warning(
                "%r: Gain's range %g to %g has no steps; offering it as a range",
                self,
                low,
                high,
            )
            return {}
        return {_factor_to_db(f): f for f in factors}

    def _raise_frame_rate(self) -> None:
        """Set ``AcquisitionFrameRate`` to its maximum on a camera that slows
        itself down.

        Such a camera lowers the rate to fit a long exposure and leaves it low
        after a shorter one, so every frame still takes as long (the IDS
        U3-36PxXCP; ids-u3-36p0xcp.md, Frame rate). The driver free-runs as fast
        as it can, so it raises the rate after anything that moves the cap.
        """
        conn = self._connection
        node = "AcquisitionFrameRate"
        if not (self._self_slowing and conn.is_writable(node)):
            return
        try:
            current, (_, high) = self._call(
                f"read {node}", lambda: (conn.get_float(node), conn.float_range(node))
            )
            if current < high:
                logger.debug("%r: raising %s from %g to %g", self, node, current, high)
                self._call(f"set {node}", lambda: conn.set_float(node, high))
        except DeviceError as exc:
            logger.warning("%s", exc)

    def _capture_frame(self, not_before: datetime | None, timeout: float) -> Frame:
        return self._grab(not_before, timeout)

    # --- Preview ---------------------------------------------------------------

    def _setup_geometry(self) -> None:
        """Before acquisition starts, find out whether the camera takes an ROI
        and put it back on the whole image.

        A camera keeps its ROI until it's power-cycled, so a preview cut short
        by a crash would otherwise leave this session capturing a crop.
        """
        conn = self._connection
        self._capture_geometry = self._applied = None
        self._limits = {}
        self._exposure_caps = {}
        self._decimations = (1,)
        nodes = ("Width", "Height", "OffsetX", "OffsetY")
        if not all(conn.is_writable(node) for node in nodes):
            return
        try:
            if conn.is_writable("DecimationVertical"):
                low, high, _ = self._int_range("DecimationVertical")
                self._decimations = tuple(d for d in (1, 2, 4, 8) if low <= d <= high)
            limits = self._enter_decimation(1)
            width, height = limits.full
            self._write_geometry(_Geometry(1, 0, 0, width, height), decimate=False)
        except DeviceError as exc:
            logger.warning("%r: previews will crop on the host: %s", self, exc)
            return
        self._capture_geometry = _Geometry(1, 0, 0, width, height)

    def _start_preview(
        self, region: PreviewRegion, max_size: int | None
    ) -> PreviewGeometry:
        capture = self._capture_geometry
        if capture is None:
            return self._start_host_preview(region, max_size)
        decimation = pick_decimation(
            region, max_size, full=self._limits[1].full, choices=self._decimations
        )
        current = self._applied or capture
        target = None
        if decimation in self._limits:
            target = self._limits[decimation].roi(region, decimation)
        exposure = self._set_exposure()
        if exposure is not None and target is not None:
            cap = self._exposure_caps.get(target.size)
            if cap is not None and cap < exposure * _EXPOSURE_KEPT:
                return self._start_whole_frame_preview(region, max_size, exposure)
        if self._applied is None:
            self._limit_before_preview = self._read_limit()
        if target is None or target.size != current.size:
            # Marked first, so a failure part-way still restores the camera.
            self._applied = current
            with self._acquisition_paused():
                limits = self._enter_decimation(decimation)
                target = limits.roi(region, decimation)
                self._write_geometry(target, decimate=False)
            self._raise_frame_rate()
            if exposure is not None and not self._keep_exposure(exposure, target):
                logger.info(
                    "%r: a smaller image would shorten the exposure; "
                    "cropping whole frames instead",
                    self,
                )
                return self._start_whole_frame_preview(region, max_size, exposure)
        elif target != current:
            self._applied = current
            self._move(target)
        if target != current:
            self._applied = target
            self._preview_floor = datetime.fromtimestamp(host_time(), UTC)
        self._crop = host_crop(
            region,
            max_size,
            sensor=self._limits[decimation].full,
            frame=(target.width, target.height),
            origin=(target.x, target.y),
        )
        return self._crop.geometry

    def _start_whole_frame_preview(
        self, region: PreviewRegion, max_size: int | None, exposure: float
    ) -> PreviewGeometry:
        """Preview from whole frames, for an exposure too long for the frame
        rate of a smaller image (the CM3; usb3-vision.md, Preview)."""
        changed = self._applied is not None
        self._stop_preview()
        if changed:
            self._keep_exposure(exposure, None)
            self._preview_floor = datetime.fromtimestamp(host_time(), UTC)
        return self._start_host_preview(region, max_size)

    def _set_exposure(self) -> float | None:
        """The exposure in the node's unit, if it's the caller's to set rather
        than the camera's (auto exposure)."""
        conn, node = self._connection, self._exposure_node
        if node is None or not (conn.is_readable(node) and conn.is_writable(node)):
            return None
        return self._call(f"read {node}", lambda: conn.get_float(node))

    def _keep_exposure(self, exposure: float, target: "_Geometry | None") -> bool:
        """Put the exposure back if a geometry change cut it. False if the
        camera no longer allows one that long."""
        conn, node = self._connection, self._exposure_node
        assert node is not None
        try:
            now, (_, high) = self._call(
                f"read {node}", lambda: (conn.get_float(node), conn.float_range(node))
            )
            if target is not None:
                self._exposure_caps[target.size] = high
            if now >= exposure * _EXPOSURE_KEPT:
                return True
            if high < exposure * _EXPOSURE_KEPT:
                return False
            self._call(f"set {node}", lambda: conn.set_float(node, exposure))
        except DeviceError as exc:
            logger.warning("%r: exposure not kept: %s", self, exc)
            return False
        return True

    def _start_host_preview(
        self, region: PreviewRegion, max_size: int | None
    ) -> PreviewGeometry:
        """Plan a crop and stride of the frames the camera already sends."""
        conn = self._connection

        def read(node: str, default: int) -> int:
            return conn.get_int(node) if conn.is_readable(node) else default

        def frames() -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
            frame = conn.get_int("Width"), conn.get_int("Height")
            origin = read("OffsetX", 0), read("OffsetY", 0)
            sensor = (
                read("SensorWidth", origin[0] + frame[0]),
                read("SensorHeight", origin[1] + frame[1]),
            )
            return sensor, frame, origin

        sensor, frame, origin = self._call("read the frame geometry", frames)
        self._crop = host_crop(
            region, max_size, sensor=sensor, frame=frame, origin=origin
        )
        return self._crop.geometry

    def _stop_preview(self) -> None:
        self._crop = self._preview_floor = None
        applied, self._applied = self._applied, None
        limit, self._limit_before_preview = self._limit_before_preview, None
        capture = self._capture_geometry
        if applied is None or capture is None:
            return
        with self._acquisition_paused():
            self._write_geometry(capture, decimate=True)
            self._restore_limit(limit)
        self._raise_frame_rate()

    def _preview_frame(self, not_before: datetime | None) -> PreviewFrame:
        assert self._crop is not None
        floor = self._preview_floor
        if floor is not None and self._timestamp_offset is not None:
            not_before = floor if not_before is None else max(not_before, floor)
        frame = self._grab(not_before, self.capture_timeout)
        return PreviewFrame(
            np.ascontiguousarray(self._crop.apply(frame.pixels)),
            frame.pixel_format,
            frame.timestamp,
            self._crop.geometry,
        )

    def _enter_decimation(self, decimation: int) -> "_GeometryLimits":
        """Switch decimation with acquisition stopped, and learn the image
        size and increments there the first time."""
        self._set_int("OffsetX", 0)
        self._set_int("OffsetY", 0)
        conn = self._connection
        for node in ("DecimationVertical", "DecimationHorizontal"):
            # The CM3's horizontal node is read-only and follows the vertical.
            if len(self._decimations) > 1 and conn.is_writable(node):
                self._set_int(node, decimation)
        if decimation not in self._limits:
            width, height = self._int_range("Width"), self._int_range("Height")
            self._limits[decimation] = _GeometryLimits(
                full=(width[1], height[1]),
                size_inc=(width[2], height[2]),
                offset_inc=(
                    self._int_range("OffsetX")[2],
                    self._int_range("OffsetY")[2],
                ),
                min_size=(width[0], height[0]),
            )
        return self._limits[decimation]

    def _write_geometry(self, geometry: "_Geometry", *, decimate: bool) -> None:
        """Write an ROI with acquisition stopped: the size while the offsets
        are zero, so it always fits."""
        if decimate:
            self._enter_decimation(geometry.decimation)
        self._set_int("Width", geometry.width)
        self._set_int("Height", geometry.height)
        self._set_int("OffsetX", geometry.x)
        self._set_int("OffsetY", geometry.y)

    def _move(self, target: "_Geometry") -> None:
        """Move an ROI of the same size, without a restart where the camera
        takes offsets while streaming."""
        conn = self._connection

        def write() -> None:
            self._set_int("OffsetX", target.x)
            self._set_int("OffsetY", target.y)

        if conn.is_writable("OffsetX") and conn.is_writable("OffsetY"):
            write()
            # For a camera that can't time frames: drop the ones already here.
            self._discard_waiting_frames()
        else:
            # IDS peak's GenTL producer locks offsets while streaming.
            with self._acquisition_paused():
                write()

    def _read_limit(self) -> int | None:
        """The throughput limit in B/s, if the camera has one switched on."""
        conn, node = self._connection, "DeviceLinkThroughputLimit"
        if not (conn.is_readable(node) and _ThroughputLimit()._active(self)):
            return None
        return self._call(f"read {node}", lambda: conn.get_int(node))

    def _restore_limit(self, saved: int | None) -> None:
        """Put back the throughput limit a smaller payload lowered, with
        acquisition stopped. Never written above its maximum, which breaks
        the CM3's stream (usb3-vision.md, Geometry).
        """
        conn, node = self._connection, "DeviceLinkThroughputLimit"
        try:
            if saved is None or self._read_limit() in (None, saved):
                return
            low, high, inc = self._int_range(node)
            if high < saved:
                # The CM3's maximum only comes back with a pixel format write.
                formats = self._enum_choices("PixelFormat", _PIXEL_FORMATS)
                current = self._read_enum("PixelFormat")
                other = next((f for f in formats if f != current), None)
                if other is not None:
                    self._set_enum("PixelFormat", other)
                    self._set_enum("PixelFormat", current)
                low, high, inc = self._int_range(node)
            if conn.is_writable(node):
                self._set_int(node, _snap(min(saved, high), low, high, inc))
        except DeviceError as exc:
            logger.warning("%r: throughput limit not restored: %s", self, exc)

    def _int_range(self, name: str) -> tuple[int, int, int]:
        conn = self._connection
        return self._call(f"read the limits of {name}", lambda: conn.int_range(name))

    def _set_int(self, name: str, value: int) -> None:
        conn = self._connection
        self._call(f"set {name} to {value}", lambda: conn.set_int(name, value))

    # --- Capture ----------------------------------------------------------------

    def _grab(self, not_before: datetime | None, timeout: float) -> Frame:
        """Take the first complete frame no older than ``not_before``.

        The camera free-runs with newest-only buffering, so the first frame is
        often from before the call. Stale and incomplete frames are released
        and the next one taken until ``timeout`` runs out; the timestamp offset
        is re-measured after every `RETRIES_BEFORE_REMEASURE` stale frames.
        """
        deadline = time.perf_counter() + timeout
        if not_before is not None:
            if self._timestamp_offset is None:
                raise DeviceError(
                    f"{self!r} can't check not_before: it can't latch its timestamp "
                    "and its library doesn't time frame arrivals"
                )
            wait = not_before.timestamp() - host_time()
            if wait >= timeout:
                raise DeviceTimeout(
                    f"{self!r}: not_before is {wait:.3g} s away, past the "
                    f"{timeout:g} s timeout"
                )
            if wait > 0:
                # No frame from before not_before would do, so don't grab any.
                time.sleep(wait)
        exposure_time = self._exposure()
        # Without an exposure time, the frame's timestamp is the device's stamp.
        exposure = exposure_time or 0.0
        retrigger_s = math.inf
        if self._software_trigger:
            turnaround = self._trigger_turnaround
            retrigger_s = exposure + (
                _RETRIGGER_TURNAROUNDS * turnaround
                if turnaround is not None
                else _PRIME_WAIT_S
            )
            # Windows' 15.6 ms clock on Python 3.12 can read not_before and the
            # trigger in one tick, and datetime's rounding then puts not_before
            # after it, so both comparisons allow the tolerance.
            tolerance = timedelta(seconds=_TRIGGER_TOLERANCE_S)
            if not_before is not None:
                not_before -= tolerance
            fired_at, self._fired_at = self._fired_at, None
            if fired_at is None or (
                not_before is not None and fired_at < not_before.timestamp()
            ):
                self._discard_waiting_frames()
                fired_at = self._fire()
            if self._timestamp_offset is not None:
                # Any frame whose exposure began before the trigger is stale.
                floor = datetime.fromtimestamp(fired_at, UTC) - tolerance
                not_before = floor if not_before is None else max(not_before, floor)
        stale = incomplete = retriggers = 0
        stalled: StreamStalled | None
        remeasured = False
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise DeviceTimeout(
                    f"{self!r}: no usable frame in {timeout:g} s "
                    f"({stale} stale, {incomplete} incomplete, "
                    f"{retriggers} triggers fired again)"
                )
            wait = min(remaining, retrigger_s)
            try:
                buffer = self._next_buffer(None if math.isinf(wait) else wait)
            except DeviceTimeout as exc:
                if wait < remaining:
                    logger.info("%r: no frame from the trigger; firing again", self)
                    self._fire()
                    retriggers += 1
                    continue
                # A timeout within one exposure can pass between healthy frames.
                if self._software_trigger or stale or incomplete or timeout <= exposure:
                    raise
                # Free-running keeps the newest frame waiting, so no frame at all
                # means nothing came since the last capture. Raised below, unchained.
                stalled = self._stalled(exc)
            else:
                stalled = None
            if stalled is not None:
                raise stalled
            try:
                taken = self._call(
                    "read a frame",
                    functools.partial(self._take, buffer, not_before, exposure),
                )
            finally:
                self._call("release a frame", buffer.release)
            if isinstance(taken, _Taken):
                metadata = {
                    EXPOSURE_TIME: exposure_time,
                    GAIN: self._gain(),
                    "device_timestamp_ns": taken.device_timestamp_ns,
                    "frame_id": taken.frame_id,
                    "retries": stale + incomplete,
                    "stale_frames": stale,
                    "incomplete_frames": incomplete,
                    "retriggers": retriggers,
                    "missing_trailer": taken.missing_trailer,
                    "timestamp_remeasured": remeasured,
                    "timestamp_source": taken.timestamp_source,
                    "bit_depth": bit_depth(taken.pixel_format),
                }
                return Frame(
                    taken.pixels,
                    taken.pixel_format,
                    taken.timestamp,
                    metadata,
                )
            if self._software_trigger:
                # The frame this capture triggered may never come: ask again.
                self._fire()
            if taken == "incomplete":
                incomplete += 1
                continue
            stale += 1
            if stale % RETRIES_BEFORE_REMEASURE == 0:
                logger.info("%r: %d stale frames; re-measuring offset", self, stale)
                self._measure_timestamp_offset()
                remeasured = True

    def _stalled(self, timeout: DeviceTimeout) -> StreamStalled:
        """The error for a free-running capture that got no frame at all."""
        counters = self._diagnostics()
        return StreamStalled(
            f"{timeout}; the camera stopped sending frames while free-running "
            f"(counters: {_counters_text(counters)})",
            counters,
        )

    def _next_buffer(self, timeout: float | None) -> Buffer:
        conn = self._connection
        try:
            buffer = self._call("get a frame", lambda: conn.grab(timeout))
        except DeviceTimeout as exc:
            if self._delivered:
                raise
            error = DeviceTimeout(f"{exc} ({self._no_frame_hint})")
        else:
            self._delivered = True
            self._last_frame_at = time.perf_counter()
            return buffer
        raise error

    def _fire(self) -> float:
        """Execute ``TriggerSoftware``; returns the host time just before."""
        conn = self._connection
        settle = self._last_frame_at + _TRIGGER_SETTLE_S - time.perf_counter()
        if settle > 0:
            time.sleep(settle)
        fired_at = host_time()
        self._call("fire a software trigger", lambda: conn.execute("TriggerSoftware"))
        self._fires.append(fired_at)
        return fired_at

    def _discard_waiting_frames(self) -> None:
        """Release frames already buffered, so a trigger's frame is the next one."""
        conn = self._connection
        for _ in range(_MAX_DISCARDED):
            try:
                buffer = self._call("discard a waiting frame", lambda: conn.grab(0))
            except DeviceTimeout:
                return
            self._last_frame_at = time.perf_counter()
            self._call("release a frame", buffer.release)
            logger.debug("%r: discarded a frame from before the trigger", self)

    def _take(
        self, buffer: Buffer, not_before: datetime | None, exposure: float
    ) -> "_Taken | str":
        """Copy a frame out of its buffer, or say why it won't do.

        The device timestamp marks the end of exposure (spinnaker.md), so the
        frame's timestamp is that minus ``exposure``, the start.
        """
        status = buffer.status()
        if status is BufferStatus.INCOMPLETE:
            logger.warning("%r: incomplete frame (%s)", self, buffer.status_text())
            return "incomplete"
        # Accepted as complete, as in pgs-scan dev (assumed; spinnaker.md).
        missing_trailer = status is BufferStatus.MISSING_TRAILER
        device_ns = buffer.device_timestamp_ns()
        source = self._timestamp_source
        triggered_at = None
        if source == "arrival":
            arrival = buffer.host_timestamp_ns()
            if arrival is not None:
                self._note_arrival(arrival, device_ns)
                triggered_at = self._first_fire_after(arrival / 1e9)
        if triggered_at is not None:
            frame_time, source = triggered_at, "trigger"
        elif self._timestamp_offset is None:
            frame_time = host_time() - exposure
        else:
            frame_time = self._timestamp_offset + device_ns / 1e9 - exposure
        timestamp = datetime.fromtimestamp(frame_time, UTC)
        if not_before is not None and timestamp < not_before:
            logger.debug("%r: dropping a frame from %s", self, timestamp)
            return "stale"
        return _Taken(
            pixels=buffer.pixels(),
            pixel_format=buffer.pixel_format(),
            timestamp=timestamp,
            device_timestamp_ns=device_ns,
            frame_id=buffer.frame_id(),
            missing_trailer=missing_trailer,
            timestamp_source=source,
        )

    def _first_fire_after(self, arrival: float) -> float | None:
        """When the trigger that exposed a frame arriving at ``arrival`` fired,
        for a camera that stamps a frame before its trigger; None otherwise.

        Some cameras send a frame's leader, and so its stamp and arrival time,
        once ready for it, before the trigger (ids-u3-36p0xcp.md, Timestamps).
        The first trigger fired after that is the one that exposed it.
        """
        if not self._software_trigger:
            return None
        return next((t for t in self._fires if t >= arrival), None)

    # --- Node access -------------------------------------------------------------

    def _call(
        self,
        action: str,
        fn: Callable[[], T],
        *,
        kind: type[DeviceError] = DeviceError,
    ) -> T:
        """Call the backend, saying which camera and what failed.

        A plain `DeviceError` from the backend becomes ``kind``; a more specific
        one (e.g. `DeviceTimeout`) keeps its type. The new error is raised
        outside the ``except`` block and isn't chained, since some libraries'
        tracebacks keep the camera alive (PySpin's stops the SDK releasing).
        """
        try:
            return fn()
        except DeviceError as exc:
            if type(exc) is not DeviceError:
                kind = type(exc)
            error = kind(f"{self!r} failed to {action}: {exc}")
        del fn
        raise error

    def _read_float(self, name: str) -> float:
        conn = self._connection
        return self._call(f"read {name}", lambda: conn.get_float(name))

    def _read_optional_float(self, name: str) -> float | None:
        if not self._connection.is_readable(name):
            return None
        return self._read_float(name)

    def _read_string(self, name: str) -> str | None:
        conn = self._connection
        if not conn.is_readable(name):
            return None
        return self._call(f"read {name}", lambda: conn.get_string(name))

    def _read_optional_string(self, name: str) -> str | None:
        """Like `_read_string`, but a failed read is logged and gives ``None``."""
        try:
            return self._read_string(name)
        except DeviceError as exc:
            logger.warning("%s", exc)
            return None

    def _read_enum(self, name: str) -> str:
        conn = self._connection
        return self._call(f"read {name}", lambda: conn.get_enum(name))

    def _enum_choices(self, name: str, supported: Iterable[str]) -> tuple[str, ...]:
        """The entries of an enumeration that are available and in ``supported``."""
        conn = self._connection
        supported = set(supported)
        entries = self._call(f"list {name} entries", lambda: conn.enum_entries(name))
        return tuple(e for e in entries if e in supported)

    def _set_enum(
        self, name: str, symbol: str, *, kind: type[DeviceError] = DeviceError
    ) -> None:
        conn = self._connection
        if not conn.is_writable(name):
            raise DeviceError(f"{self!r} can't set {name}: it isn't writable")
        entries = self._call(f"list {name} entries", lambda: conn.enum_entries(name))
        if symbol not in entries:
            raise DeviceError(f"{self!r} has no {name} {symbol!r}")
        self._call(
            f"set {name} to {symbol}", lambda: conn.set_enum(name, symbol), kind=kind
        )


@dataclass(frozen=True)
class _Geometry:
    """An ROI, in pixels of the image at its decimation."""

    decimation: int
    x: int
    y: int
    width: int
    height: int

    @property
    def size(self) -> tuple[int, int, int]:
        """What can't change without an acquisition restart."""
        return self.decimation, self.width, self.height


@dataclass(frozen=True)
class _GeometryLimits:
    """The whole image and the ROI increments at one decimation."""

    full: tuple[int, int]
    size_inc: tuple[int, int]
    offset_inc: tuple[int, int]
    min_size: tuple[int, int]

    def roi(self, region: PreviewRegion, decimation: int) -> _Geometry:
        x, y, width, height = hardware_roi(
            region,
            full=self.full,
            size_inc=self.size_inc,
            offset_inc=self.offset_inc,
            min_size=self.min_size,
        )
        return _Geometry(decimation, x, y, width, height)


# --- Settings ----------------------------------------------------------------


class _Descriptor:
    """How one setting is read from and written to a camera's nodes.

    By default a setting is read-only and adds nothing to frame metadata.
    """

    name: str

    def read(self, camera: GenICamCamera) -> Setting | None:
        """The setting now, or None if the camera lacks it."""
        raise NotImplementedError

    def write(self, camera: GenICamCamera, value: SettingValue) -> None:
        """Write a value `Setting.validate` has accepted."""
        raise AssertionError(f"{self.name} is read-only")

    def current(self, camera: GenICamCamera) -> float | None:
        """Just the value, for frame metadata (exposure and gain only)."""
        return None


@dataclass(frozen=True)
class _Float(_Descriptor):
    """A float node, offered in its own unit: the node holds value × scale."""

    name: str
    node: str
    unit: str
    scale: float = 1.0
    # Whether a write can move a self-slowing camera's frame-rate cap.
    moves_rate: bool = False

    def read(self, camera: GenICamCamera) -> Setting | None:
        conn, node = camera._connection, self.node
        if not conn.is_readable(node):
            return None
        value, (low, high) = camera._call(
            f"read {node}", lambda: (conn.get_float(node), conn.float_range(node))
        )
        return Setting(
            self.name,
            self._from_native(value),
            minimum=self._from_native(low),
            maximum=self._from_native(high),
            unit=self.unit,
            read_only=not conn.is_writable(node),
        )

    def write(self, camera: GenICamCamera, value: SettingValue) -> None:
        conn, node = camera._connection, self.node
        low, high = camera._call(
            f"read the limits of {self.name}", lambda: conn.float_range(node)
        )
        # Clamp, so a limit that went through the unit conversion still fits.
        native = min(max(self._to_native(float(value)), low), high)
        camera._call(
            f"set {self.name}",
            lambda: conn.set_float(node, native),
            kind=CommandRejected,
        )
        if self.moves_rate:
            camera._raise_frame_rate()

    def current(self, camera: GenICamCamera) -> float | None:
        native = camera._read_optional_float(self.node)
        return None if native is None else self._from_native(native)

    def _from_native(self, native: float) -> float:
        return native / self.scale

    def _to_native(self, value: float) -> float:
        return value * self.scale


@dataclass(frozen=True)
class _LinearGain(_Float):
    """``gain`` in dB on a camera whose ``Gain`` node is a factor."""

    name: str = GAIN
    node: str = "Gain"
    unit: str = "dB"

    def _from_native(self, native: float) -> float:
        return _factor_to_db(native)

    def _to_native(self, value: float) -> float:
        return 10 ** (value / 20)


@dataclass(frozen=True)
class _GainSteps(_Descriptor):
    """``gain`` on a linear-gain camera that keeps only some factors and rounds
    any other down unannounced, so it's offered as the dB of each."""

    steps: Mapping[float, float]  # dB to factor
    name: str = GAIN

    def read(self, camera: GenICamCamera) -> Setting | None:
        conn = camera._connection
        if not conn.is_readable("Gain"):
            return None
        return Setting(
            GAIN,
            self._db(camera._read_float("Gain")),
            choices=tuple(self.steps),
            unit="dB",
            read_only=not conn.is_writable("Gain"),
        )

    def write(self, camera: GenICamCamera, value: SettingValue) -> None:
        conn = camera._connection
        # The exact factor: one a hair below it would be rounded down a step.
        factor = self.steps[float(value)]
        camera._call(
            "set gain", lambda: conn.set_float("Gain", factor), kind=CommandRejected
        )

    def current(self, camera: GenICamCamera) -> float | None:
        factor = camera._read_optional_float("Gain")
        return None if factor is None else self._db(factor)

    def _db(self, factor: float) -> float:
        return min(self.steps, key=lambda db: abs(self.steps[db] - factor))


@dataclass(frozen=True)
class _Enum(_Descriptor):
    """An enumeration node, offering the entries in ``supported``."""

    name: str
    node: str
    supported: Iterable[str]
    # Written with acquisition paused, since the camera only takes it stopped.
    paused: bool = False

    def read(self, camera: GenICamCamera) -> Setting | None:
        conn = camera._connection
        if not conn.is_readable(self.node):
            return None
        return Setting(
            self.name,
            camera._read_enum(self.node),
            choices=camera._enum_choices(self.node, self.supported),
            read_only=not (self.paused or conn.is_writable(self.node)),
        )

    def write(self, camera: GenICamCamera, value: SettingValue) -> None:
        if not self.paused:
            camera._set_enum(self.node, str(value), kind=CommandRejected)
            return
        with camera._acquisition_paused():
            camera._set_enum(self.node, str(value), kind=CommandRejected)
        camera._raise_frame_rate()


@dataclass(frozen=True)
class _FrameSize(_Descriptor):
    """A capture's width or height: the node, or the capture geometry on a
    camera whose ROI preview changes."""

    name: str
    node: str

    def read(self, camera: GenICamCamera) -> Setting | None:
        capture = camera._capture_geometry
        if capture is not None:
            value = capture.width if self.node == "Width" else capture.height
            return Setting(self.name, value, unit="px", read_only=True)
        return _ReadOnly(self.name, (self.node,), "px", integer=True).read(camera)


@dataclass(frozen=True)
class _ReadOnly(_Descriptor):
    """A number read from the first of ``nodes`` the camera can read."""

    name: str
    nodes: tuple[str, ...]
    unit: str
    scale: float = 1.0
    integer: bool = False

    def read(self, camera: GenICamCamera) -> Setting | None:
        conn = camera._connection
        node = next((n for n in self.nodes if conn.is_readable(n)), None)
        if node is None:
            return None
        if self.integer:
            value: float = camera._call(f"read {node}", lambda: conn.get_int(node))
        else:
            value = camera._read_float(node)
        if self.scale != 1.0:
            value /= self.scale
        return Setting(self.name, value, unit=self.unit, read_only=True)


# Camera nodes `diagnostics()` reports, by its key.
_CAMERA_COUNTERS = (
    ("TransmitFailureCount", "transmit_failures"),
    ("USB3LinkRecoveryCount", "link_recoveries"),
)

# Nominal signalling rates of U3VCurrentSpeed's entries, in MB/s.
_USB_SPEEDS = {
    "LowSpeed": 0.1875,
    "FullSpeed": 1.5,
    "HighSpeed": 60.0,
    "SuperSpeed": 625.0,
}


@dataclass(frozen=True)
class _LinkSpeed(_Descriptor):
    """The link's speed in MB/s, from ``DeviceLinkSpeed`` or, on cameras without
    it (e.g. the CM3-U3-50S5M), the nominal rate of ``U3VCurrentSpeed``."""

    name: str = "link_speed"

    def read(self, camera: GenICamCamera) -> Setting | None:
        found = _ReadOnly(
            self.name, ("DeviceLinkSpeed",), "MB/s", _BYTES_PER_MB, integer=True
        ).read(camera)
        if found is not None or not camera._connection.is_readable("U3VCurrentSpeed"):
            return found
        entry = camera._read_enum("U3VCurrentSpeed")
        if entry not in _USB_SPEEDS:
            logger.debug("%r: unknown U3VCurrentSpeed %s", camera, entry)
            return None
        return Setting(self.name, _USB_SPEEDS[entry], unit="MB/s", read_only=True)


@dataclass(frozen=True)
class _ThroughputLimit(_Descriptor):
    """The link's throughput limit in MB/s; ``None`` while a mode turns it off."""

    name: str = THROUGHPUT_LIMIT
    node: str = "DeviceLinkThroughputLimit"
    mode: str = "DeviceLinkThroughputLimitMode"

    def read(self, camera: GenICamCamera) -> Setting | None:
        conn, node = camera._connection, self.node
        if not conn.is_readable(node):
            return None
        value, (low, high, inc) = camera._call(
            f"read {node}", lambda: (conn.get_int(node), conn.int_range(node))
        )
        return Setting(
            self.name,
            # Writing a value turns the limit on.
            value / _BYTES_PER_MB if self._active(camera) else None,
            minimum=low / _BYTES_PER_MB,
            maximum=_snap(high, low, high, inc) / _BYTES_PER_MB,
            step=inc / _BYTES_PER_MB,
            unit="MB/s",
        )

    def write(self, camera: GenICamCamera, value: SettingValue) -> None:
        conn, node = camera._connection, self.node
        if not self._active(camera):
            camera._set_enum(self.mode, "On", kind=CommandRejected)
        low, high, inc = camera._call(
            f"read the limits of {node}", lambda: conn.int_range(node)
        )
        native = _snap(float(value) * _BYTES_PER_MB, low, high, inc)

        def write() -> None:
            camera._call(
                f"set {node} to {native} B/s",
                lambda: conn.set_int(node, native),
                kind=CommandRejected,
            )

        if conn.is_writable(node):
            write()
        else:
            # Some cameras may only take it while stopped (unverified; spinnaker.md).
            with camera._acquisition_paused():
                if not conn.is_writable(node):
                    raise CommandRejected(
                        f"{camera!r} won't accept {node}, even stopped"
                    )
                write()
        if camera._limit_before_preview is not None:
            # Set during preview, so this is what stopping should leave.
            camera._limit_before_preview = native
        camera._raise_frame_rate()

    def _active(self, camera: GenICamCamera) -> bool:
        if not camera._connection.is_readable(self.mode):
            return True
        return camera._read_enum(self.mode) == "On"


@dataclass(frozen=True)
class _Trigger(_Descriptor):
    """``"Off"`` or ``"Software"``: whether each capture fires a software trigger."""

    name: str = TRIGGER

    def read(self, camera: GenICamCamera) -> Setting | None:
        conn = camera._connection
        if not (conn.is_readable("TriggerMode") and conn.is_readable("TriggerSource")):
            return None
        if not conn.has("TriggerSoftware"):
            return None
        value: str | None = None
        if camera._read_enum("TriggerMode") == "Off":
            value = "Off"
        elif camera._read_enum("TriggerSource") == "Software" and (
            self._selector(camera) in (None, "FrameStart")
        ):
            value = "Software"
        # Any other state (e.g. a hardware line) reads None; writing leaves it.
        return Setting(TRIGGER, value, choices=_TRIGGER_MODES)

    def write(self, camera: GenICamCamera, value: SettingValue) -> None:
        # TriggerSource is only writable while TriggerMode is Off (Spinnaker's
        # Trigger example); the mode itself is switched with acquisition stopped.
        with camera._acquisition_paused():
            camera._set_enum("TriggerMode", "Off", kind=CommandRejected)
            camera._software_trigger = False
            camera._fired_at = None
            if value == "Software":
                if self._selector(camera) not in (None, "FrameStart"):
                    camera._set_enum(
                        "TriggerSelector", "FrameStart", kind=CommandRejected
                    )
                camera._set_enum("TriggerSource", "Software", kind=CommandRejected)
                camera._set_enum("TriggerMode", "On", kind=CommandRejected)
                camera._software_trigger = True

    def _selector(self, camera: GenICamCamera) -> str | None:
        if not camera._connection.is_readable("TriggerSelector"):
            return None
        return camera._read_enum("TriggerSelector")


@dataclass(frozen=True)
class _Taken:
    """A usable frame, copied out of its buffer."""

    pixels: npt.NDArray[Any]
    pixel_format: str
    timestamp: datetime
    device_timestamp_ns: int
    frame_id: int
    missing_trailer: bool
    timestamp_source: str | None


def _times(buffer: Buffer) -> tuple[BufferStatus, int | None, int]:
    return buffer.status(), buffer.host_timestamp_ns(), buffer.device_timestamp_ns()


def pfnc_name(code: int) -> str:
    """The SFNC name of a pixel format the core can shape, from its PFNC code.

    Raises:
        DeviceError: The format isn't one the core can shape.
    """
    try:
        return _PFNC[code]
    except KeyError:
        raise DeviceError(f"unsupported pixel format 0x{code:08x}") from None


def bit_depth(pixel_format: str) -> int:
    """Significant bits per pixel value in a format the core can shape.

    Deeper formats come as ``uint16``, with the bits in the low end: a 10-bit
    pixel runs from 0 to 1023.
    """
    if pixel_format == "Mono10g40IDS":
        return 10
    return 16 if pixel_format.endswith("16") else 8


def shaped_pixels(
    data: Any, pixel_format: str, width: int, height: int
) -> npt.NDArray[Any]:
    """A copy of a frame's bytes as an array shaped by its pixel format."""
    if pixel_format == "Mono10g40IDS":
        return _unpack_10g40(data, width, height)
    dtype = np.uint16 if pixel_format.endswith("16") else np.uint8
    shape: tuple[int, ...] = (height, width)
    if pixel_format == "RGB8":
        shape += (3,)
    count = math.prod(shape)
    return np.frombuffer(data, dtype=dtype, count=count).reshape(shape).copy()


def _unpack_10g40(data: Any, width: int, height: int) -> npt.NDArray[np.uint16]:
    """Unpack IDS's Mono10g40IDS: per 4 pixels, their upper 8 bits in 4 bytes,
    then one byte of low 2-bit pairs, pixel 0's lowest (verified on the
    U3-36PxXCP-M; ids-u3-36p0xcp.md, Pixel formats)."""
    count = width * height
    groups = np.frombuffer(data, dtype=np.uint8, count=count * 5 // 4).reshape(-1, 5)
    pixels = groups[:, :4].astype(np.uint16) << 2
    low = groups[:, 4:5]
    pixels |= ((low >> np.array([0, 2, 4, 6], dtype=np.uint8)) & 3).astype(np.uint16)
    return pixels.reshape(height, width).astype(np.uint16, copy=False)


def _binary_steps(low: float, high: float, bits: int) -> list[float]:
    """Every value from ``low`` to ``high`` with at most ``bits`` binary places
    after its leading one: for 4 bits, 1, 1.0625, …, 1.9375, 2, 2.125, …

    The ends are matched loosely, so a limit read as 1.0000001 still includes 1.
    """
    if not 0 < low <= high:
        return []
    low, high = low * (1 - 1e-6), high * (1 + 1e-6)
    octave = math.floor(math.log2(low))
    found: list[float] = []
    while (base := 2.0**octave) <= high:
        found += [
            v for k in range(2**bits) if low <= (v := base * (1 + k / 2**bits)) <= high
        ]
        octave += 1
    return found


def _factor_to_db(factor: float) -> float:
    """A linear gain factor in dB; 1 is 0 dB."""
    return 20 * math.log10(factor) if factor > 0 else -math.inf


def _snap(value: float, low: int, high: int, inc: int) -> int:
    """The nearest of ``low + k * inc`` within ``[low, high]``, as an int."""
    inc = max(int(inc), 1)
    top = low + (high - low) // inc * inc
    k = round((value - low) / inc)
    return int(min(max(low + k * inc, low), top))
