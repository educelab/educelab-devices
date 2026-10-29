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
)

__all__ = [
    "ARRIVAL_SAMPLES",
    "PREVIEW_MAX_SIZE",
    "RETRIES_BEFORE_REMEASURE",
    "THROUGHPUT_LIMIT",
    "TIMESTAMP_SAMPLES",
    "TRIGGER",
    "Backend",
    "Buffer",
    "BufferStatus",
    "Connection",
    "GenICamCamera",
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

    def start_acquisition(self) -> None: ...

    def stop_acquisition(self) -> None:
        """Stop acquiring. Does nothing if already stopped."""
        ...

    def grab(self, timeout: float | None) -> Buffer:
        """The newest frame, waiting up to ``timeout`` seconds for one.

        ``None`` waits indefinitely, and ``0`` takes a frame only if one is
        already waiting.

        Raises:
            DeviceTimeout: No frame came in time.
        """
        ...

    def close(self) -> None:
        """Stop acquiring and let go of the camera. Does nothing if closed.

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
PREVIEW_MAX_SIZE = 1280

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
# "Once" isn't offered: it reverts to "Off" by itself, so it never reads back.
_AUTO_MODES = ("Off", "Continuous")
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
        self._timestamp_offset: float | None = None
        self._timestamp_source: str | None = None
        self._arrival_gaps: deque[float] = deque(maxlen=ARRIVAL_SAMPLES)
        self._delivered = False
        self._software_trigger = False
        self._fired_at: float | None = None
        self._fires: deque[float] = deque(maxlen=_FIRES_KEPT)
        self._last_frame_at = -math.inf
        self._trigger_turnaround: float | None = None

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

    # --- Lifecycle -------------------------------------------------------------

    @property
    def _connection(self) -> Connection:
        if self._conn is None:
            raise NotConnected(f"{self!r} is not open")
        return self._conn

    def _open(self) -> None:
        self._conn = self._call("open", lambda: self._backend.open(self._serial))
        if self._conn is None:
            raise DeviceNotFound(self._not_found_message())
        try:
            self._model = self._read_string("DeviceModelName") or self._model
            self._vendor = self._read_optional_string("DeviceVendorName")
            self._firmware = self._read_string("DeviceFirmwareVersion")
            self._own_serial = self._read_string("DeviceSerialNumber")
            self._configure()
            self._delivered = False
            self._software_trigger = False
            self._fired_at = None
            self._call("begin acquisition", self._conn.start_acquisition)
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
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except DeviceError as exc:
                problems.append(str(exc))
        self._timestamp_offset = None
        self._timestamp_source = None
        self._arrival_gaps.clear()
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

    @contextmanager
    def _acquisition_paused(self) -> Iterator[None]:
        conn = self._connection
        self._call("end acquisition", conn.stop_acquisition)
        try:
            yield
        finally:
            self._call("begin acquisition", conn.start_acquisition)
        if self._software_trigger:
            self._prime_trigger()

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
        found = (
            self._exposure_setting(),
            self._enum_setting("ExposureAuto", _AUTO_MODES),
            self._float_setting(GAIN, "Gain", "dB"),
            self._enum_setting("GainAuto", _AUTO_MODES),
            # Read-only while acquiring, but _set_setting pauses acquisition.
            self._enum_setting(
                "PixelFormat", _PIXEL_FORMATS, name=PIXEL_FORMAT, writable=True
            ),
            # Read-only even where the camera could crop: the driver sets no ROI.
            self._pixels_setting(WIDTH, "Width"),
            self._pixels_setting(HEIGHT, "Height"),
            self._pixels_setting(SENSOR_WIDTH, "SensorWidth"),
            self._pixels_setting(SENSOR_HEIGHT, "SensorHeight"),
            self._throughput_limit_setting(),
            self._trigger_setting(),
            self._read_only_number(
                "current_throughput",
                "DeviceLinkCurrentThroughput",
                "MB/s",
                _BYTES_PER_MB,
                integer=True,
            ),
            self._read_only_number(
                "resulting_frame_rate", "AcquisitionResultingFrameRate", "Hz"
            ),
        )
        return {s.name: s for s in found if s is not None}

    def _set_setting(self, name: str, value: SettingValue) -> None:
        logger.debug("%r: %s = %r", self, name, value)
        conn = self._connection
        if name in (EXPOSURE_TIME, GAIN):
            node, scale = (
                (str(self._exposure_node), _US_PER_S)
                if name == EXPOSURE_TIME
                else ("Gain", 1.0)
            )
            # Clamp, so a limit that went through the unit conversion still fits.
            low, high = self._call(
                f"read the limits of {name}", lambda: conn.float_range(node)
            )
            native = min(max(float(value) * scale, low), high)
            self._call(
                f"set {name}",
                lambda: conn.set_float(node, native),
                kind=CommandRejected,
            )
        elif name == PIXEL_FORMAT:
            with self._acquisition_paused():
                self._set_enum("PixelFormat", str(value), kind=CommandRejected)
        elif name == THROUGHPUT_LIMIT:
            self._set_throughput_limit(float(value))
        elif name == TRIGGER:
            self._set_trigger(str(value))
        else:
            self._set_enum(name, str(value), kind=CommandRejected)

    def _exposure_setting(self) -> Setting | None:
        if self._exposure_node is None:
            return None
        return self._float_setting(EXPOSURE_TIME, self._exposure_node, "s", _US_PER_S)

    def _exposure(self) -> float | None:
        """The exposure time in seconds, or None if the camera has no such node."""
        if self._exposure_node is None:
            return None
        return self._read_float(self._exposure_node) / _US_PER_S

    def _throughput_limit_setting(self) -> Setting | None:
        conn = self._connection
        node = "DeviceLinkThroughputLimit"
        if not conn.is_readable(node):
            return None
        value, (low, high, inc) = self._call(
            f"read {node}", lambda: (conn.get_int(node), conn.int_range(node))
        )
        return Setting(
            THROUGHPUT_LIMIT,
            # None while a limit mode turns the limit off; writing turns it on.
            value / _BYTES_PER_MB if self._throughput_limit_active() else None,
            minimum=low / _BYTES_PER_MB,
            maximum=_snap(high, low, high, inc) / _BYTES_PER_MB,
            step=inc / _BYTES_PER_MB,
            unit="MB/s",
        )

    def _throughput_limit_active(self) -> bool:
        if not self._connection.is_readable("DeviceLinkThroughputLimitMode"):
            return True
        return self._read_enum("DeviceLinkThroughputLimitMode") == "On"

    def _set_throughput_limit(self, mb_per_s: float) -> None:
        conn = self._connection
        node = "DeviceLinkThroughputLimit"
        if not self._throughput_limit_active():
            self._set_enum("DeviceLinkThroughputLimitMode", "On", kind=CommandRejected)
        low, high, inc = self._call(
            f"read the limits of {node}", lambda: conn.int_range(node)
        )
        native = _snap(mb_per_s * _BYTES_PER_MB, low, high, inc)

        def write() -> None:
            self._call(
                f"set {node} to {native} B/s",
                lambda: conn.set_int(node, native),
                kind=CommandRejected,
            )

        if conn.is_writable(node):
            write()
            return
        # Some cameras may only take it while stopped (unverified; spinnaker.md).
        with self._acquisition_paused():
            if not conn.is_writable(node):
                raise CommandRejected(f"{self!r} won't accept {node}, even stopped")
            write()

    def _trigger_setting(self) -> Setting | None:
        conn = self._connection
        if not (conn.is_readable("TriggerMode") and conn.is_readable("TriggerSource")):
            return None
        if not conn.has("TriggerSoftware"):
            return None
        value: str | None = None
        if self._read_enum("TriggerMode") == "Off":
            value = "Off"
        elif self._read_enum("TriggerSource") == "Software" and (
            self._trigger_selector() in (None, "FrameStart")
        ):
            value = "Software"
        # Any other state (e.g. a hardware line) reads None; writing leaves it.
        return Setting(TRIGGER, value, choices=_TRIGGER_MODES)

    def _trigger_selector(self) -> str | None:
        if not self._connection.is_readable("TriggerSelector"):
            return None
        return self._read_enum("TriggerSelector")

    def _set_trigger(self, value: str) -> None:
        # TriggerSource is only writable while TriggerMode is Off (Spinnaker's
        # Trigger example); the mode itself is switched with acquisition stopped.
        with self._acquisition_paused():
            self._set_enum("TriggerMode", "Off", kind=CommandRejected)
            self._software_trigger = False
            self._fired_at = None
            if value == "Software":
                if self._trigger_selector() not in (None, "FrameStart"):
                    self._set_enum(
                        "TriggerSelector", "FrameStart", kind=CommandRejected
                    )
                self._set_enum("TriggerSource", "Software", kind=CommandRejected)
                self._set_enum("TriggerMode", "On", kind=CommandRejected)
                self._software_trigger = True

    def _read_only_number(
        self,
        name: str,
        node: str,
        unit: str,
        scale: float = 1.0,
        *,
        integer: bool = False,
    ) -> Setting | None:
        conn = self._connection
        if not conn.is_readable(node):
            return None
        read = conn.get_int if integer else conn.get_float
        value = self._call(f"read {node}", lambda: read(node))
        return Setting(name, float(value) / scale, unit=unit, read_only=True)

    def _pixels_setting(self, name: str, node: str) -> Setting | None:
        conn = self._connection
        if not conn.is_readable(node):
            return None
        value = self._call(f"read {node}", lambda: conn.get_int(node))
        return Setting(name, value, unit="px", read_only=True)

    def _float_setting(
        self, name: str, node: str, unit: str, scale: float = 1.0
    ) -> Setting | None:
        conn = self._connection
        if not conn.is_readable(node):
            return None
        value, (low, high) = self._call(
            f"read {node}", lambda: (conn.get_float(node), conn.float_range(node))
        )
        return Setting(
            name,
            value / scale,
            minimum=low / scale,
            maximum=high / scale,
            unit=unit,
            read_only=not conn.is_writable(node),
        )

    def _enum_setting(
        self,
        node: str,
        supported: Iterable[str],
        *,
        name: str | None = None,
        writable: bool = False,
    ) -> Setting | None:
        conn = self._connection
        if not conn.is_readable(node):
            return None
        return Setting(
            name or node,
            self._read_enum(node),
            choices=self._enum_choices(node, supported),
            read_only=not (writable or conn.is_writable(node)),
        )

    def _capture_frame(self, not_before: datetime | None, timeout: float) -> Frame:
        return self._grab(not_before, timeout)

    def _preview_frame(self) -> npt.NDArray[np.uint8]:
        """The newest frame as 8 bits, subsampled to `PREVIEW_MAX_SIZE`."""
        frame = self._grab(None, self.capture_timeout)
        pixels = frame.pixels
        if pixels.dtype == np.uint16:
            pixels = pixels >> (frame.metadata["bit_depth"] - 8)
        stride = -(-max(pixels.shape[:2]) // PREVIEW_MAX_SIZE)
        return np.ascontiguousarray(pixels[::stride, ::stride], dtype=np.uint8)

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
            except DeviceTimeout:
                if wait >= remaining:
                    raise
                logger.info("%r: no frame from the trigger; firing again", self)
                self._fire()
                retriggers += 1
                continue
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
                    GAIN: self._read_optional_float("Gain"),
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


def _snap(value: float, low: int, high: int, inc: int) -> int:
    """The nearest of ``low + k * inc`` within ``[low, high]``, as an int."""
    inc = max(int(inc), 1)
    top = low + (high - low) // inc * inc
    k = round((value - low) / inc)
    return int(min(max(low + k * inc, low), top))
