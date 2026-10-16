"""FLIR/Teledyne machine-vision cameras, through the Spinnaker SDK (PySpin).

PySpin is imported on first use, so this module imports without it. See
``docs/hardware/spinnaker.md`` for installing the SDK.
"""

import functools
import logging
import math
import statistics
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import ModuleType, TracebackType
from typing import Any, Self, TypeVar

import numpy as np
import numpy.typing as npt

from educelab.devices.cameras.base import (
    EXPOSURE_TIME,
    GAIN,
    PIXEL_FORMAT,
    Frame,
    FrameCamera,
    Setting,
    SettingValue,
)
from educelab.devices.device import DeviceInfo, synchronized
from educelab.devices.errors import (
    CommandRejected,
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    NotConnected,
    import_optional,
)

__all__ = [
    "MIN_SPINNAKER_VERSION",
    "PREVIEW_MAX_SIZE",
    "PYSPIN_INSTALL_HINT",
    "RETRIES_BEFORE_REMEASURE",
    "TIMESTAMP_SAMPLES",
    "SpinnakerCamera",
    "SpinnakerSystem",
    "load_pyspin",
]

logger = logging.getLogger(__name__)

BACKEND = "spinnaker"

T = TypeVar("T")

# Latch samples whose median gives the device-to-host timestamp offset; the
# count comes from pgs-scan (and multi_pyspin before it). See the hardware doc.
TIMESTAMP_SAMPLES = 20
# Newer cameras name the latched value "Timestamp", older ones
# "TimestampLatchValue" (docs/hardware/spinnaker.md, Protocol).
_TIMESTAMP_NODES = ("Timestamp", "TimestampLatchValue")

# Stale frames a capture drops before re-measuring the timestamp offset, as in
# pgs-scan dev. The capture's timeout bounds the retries. See the hardware doc.
RETRIES_BEFORE_REMEASURE = 5

# Longest side of a preview, in pixels; larger frames are subsampled.
PREVIEW_MAX_SIZE = 1280

# Pixel formats whose arrays the driver knows how to shape. Others (e.g.
# packed 12-bit) would come back as raw bytes.
_PIXEL_FORMATS = frozenset(
    {"Mono8", "Mono16", "RGB8"}
    | {f"Bayer{order}{bits}" for order in ("RG", "GB", "GR", "BG") for bits in (8, 16)}
)
# "Once" isn't offered: it reverts to "Off" by itself, so it never reads back.
_AUTO_MODES = ("Off", "Continuous")
_US_PER_S = 1e6


# The only SDK the driver's PySpin names have been checked against, and the first
# with Python 3.12 wheels for macOS and Windows. See the hardware doc.
MIN_SPINNAKER_VERSION = (4, 4, 0)

_INSTALL_DOCS = (
    "See docs/hardware/spinnaker.md: https://github.com/educelab/educelab-devices/"
    "blob/main/docs/hardware/spinnaker.md"
)
PYSPIN_INSTALL_HINT = (
    "PySpin is not on PyPI. Install the Spinnaker SDK "
    f"{'.'.join(map(str, MIN_SPINNAKER_VERSION))} or newer, then the PySpin wheel "
    f"from the same SDK release (Python 3.12). {_INSTALL_DOCS}"
)


def load_pyspin() -> ModuleType:
    """Import PySpin, or raise an `ImportError` that explains how to install it."""
    return import_optional("PySpin", hint=PYSPIN_INSTALL_HINT)


class _SharedSystem:
    """The ``System`` instance taken from one PySpin module, and its users."""

    def __init__(self, pyspin: ModuleType, system: Any) -> None:
        self.pyspin = pyspin
        self.system = system
        self.users = 0


# One entry per PySpin module (the real one, or a fake per test), keyed by id().
_shared_lock = threading.Lock()
_shared: dict[int, _SharedSystem] = {}


class SpinnakerSystem:
    """A handle on PySpin's process-wide ``System``, which enumerates cameras.

    Every open handle shares one ``System``: the first to open takes it, and
    the last to close releases it. Each camera holds a handle while open, so
    closing yours never pulls the system out from under an open camera.

    Handles are thread-safe. The constructor doesn't touch the SDK.

    Args:
        pyspin: A module to use in place of PySpin, for tests.
    """

    def __init__(self, pyspin: ModuleType | None = None) -> None:
        self._pyspin_override = pyspin
        self._lock = threading.RLock()
        self._entry: _SharedSystem | None = None

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    @property
    def is_open(self) -> bool:
        """Whether this handle holds the system."""
        return self._entry is not None

    def open(self) -> None:
        """Take the PySpin ``System``. Does nothing if already open.

        Raises:
            ImportError: PySpin isn't installed, or its SDK is older than
                `MIN_SPINNAKER_VERSION`.
            DeviceError: The SDK couldn't start.
        """
        with self._lock:
            if self._entry is not None:
                return
            pyspin = self._pyspin_override or load_pyspin()
            with _shared_lock:
                entry = _shared.get(id(pyspin))
                if entry is None:
                    try:
                        system = pyspin.System.GetInstance()
                    except pyspin.SpinnakerException as exc:
                        raise DeviceError(
                            f"Spinnaker SDK failed to start: {exc}"
                        ) from exc
                    _require_version(pyspin, system)
                    entry = _shared[id(pyspin)] = _SharedSystem(pyspin, system)
                    logger.debug("took the Spinnaker system")
                entry.users += 1
            self._entry = entry

    def close(self) -> None:
        """Let go of the system. Does nothing if already closed.

        The handle is closed afterwards even if releasing the system fails.

        Raises:
            DeviceError: This was the last handle, and the SDK refused to release
                the system because a camera or camera list is still held.
        """
        with self._lock:
            entry, self._entry = self._entry, None
            if entry is None:
                return
            with _shared_lock:
                entry.users -= 1
                if entry.users:
                    return
                del _shared[id(entry.pyspin)]
                system, entry.system = entry.system, None
                try:
                    system.ReleaseInstance()
                except entry.pyspin.SpinnakerException as exc:
                    raise DeviceError(
                        f"could not release the Spinnaker system: {exc}"
                    ) from exc
                logger.debug("released the Spinnaker system")

    @property
    def library_version(self) -> str:
        """The Spinnaker SDK version, e.g. ``"4.4.0.123"``.

        Raises:
            NotConnected: The handle isn't open.
        """
        with self._lock:
            entry = self._require_open()
            v = entry.system.GetLibraryVersion()
            return f"{v.major}.{v.minor}.{v.type}.{v.build}"

    def discover(self) -> list[DeviceInfo]:
        """List attached cameras without initializing them.

        Opens the handle for the duration if it is closed. Cameras that can't
        be read (e.g. unplugged mid-listing) are skipped.

        Raises:
            ImportError: PySpin isn't installed.
            DeviceError: The SDK couldn't enumerate cameras.
        """
        with self._lock, self._opened() as entry:
            pyspin = entry.pyspin
            try:
                cameras = entry.system.GetCameras()
            except pyspin.SpinnakerException as exc:
                raise DeviceError(f"could not list Spinnaker cameras: {exc}") from exc
            try:
                # Indexing rather than iterating leaves no camera reference behind,
                # which would stop the system from being released.
                found = [
                    _describe(pyspin, cameras.GetByIndex(i))
                    for i in range(cameras.GetSize())
                ]
            finally:
                # Only after visiting every camera (pgs-scan #90).
                cameras.Clear()
        return [info for info in found if info is not None]

    @property
    def _module(self) -> Any:
        """The PySpin module this handle uses."""
        return self._require_open().pyspin

    def _camera(self, serial: str) -> Any | None:
        """A pointer to the camera with this serial number, or None.

        The caller owns the pointer and must drop it before the system can be
        released.
        """
        with self._lock:
            entry = self._require_open()
            try:
                cameras = entry.system.GetCameras()
            except entry.pyspin.SpinnakerException as exc:
                error = DeviceError(f"could not list Spinnaker cameras: {exc}")
            else:
                try:
                    return _get_by_serial(entry.pyspin, cameras, serial)
                finally:
                    cameras.Clear()
            raise error

    def _require_open(self) -> _SharedSystem:
        if self._entry is None:
            raise NotConnected(f"{self!r} is not open; call open() first")
        return self._entry

    @contextmanager
    def _opened(self) -> Iterator[_SharedSystem]:
        was_open = self.is_open
        self.open()
        try:
            yield self._require_open()
        finally:
            if not was_open:
                self.close()

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _describe(pyspin: Any, cam: Any) -> DeviceInfo | None:
    """Identify a camera from its transport-layer nodes, which need no ``Init()``."""

    def read(name: str) -> str | None:
        node = getattr(cam.TLDevice, name)
        return str(node.ToString()) if pyspin.IsReadable(node) else None

    try:
        serial = read("DeviceSerialNumber")
        model = read("DeviceModelName")
    except pyspin.SpinnakerException as exc:
        # Log the text, not the exception: a kept log record would hold its
        # traceback, and with it ``cam``, which blocks releasing the system.
        logger.warning("skipping a Spinnaker camera that can't be read: %s", str(exc))
        return None
    if serial is None:
        logger.warning("Spinnaker camera %r has no readable serial number", model)
    return DeviceInfo(BACKEND, model or "Spinnaker camera", serial_number=serial)


def _get_by_serial(pyspin: Any, cameras: Any, serial: str) -> Any | None:
    try:
        cam = cameras.GetBySerial(serial)
    except pyspin.SpinnakerException as exc:
        logger.debug("no Spinnaker camera %s: %s", serial, str(exc))
        return None
    return cam if cam.IsValid() else None


class SpinnakerCamera(FrameCamera):
    """One FLIR/Teledyne camera, addressed by serial number.

    ``open()`` initializes the camera, measures the offset between its
    timestamp counter and the host clock, and starts free-running acquisition
    that keeps only the newest frame, which is the fastest way to capture.
    ``close()`` stops acquiring and de-initializes it. Changing
    ``pixel_format`` pauses acquisition briefly, since cameras only accept it
    while stopped.

    Every PySpin error becomes a `DeviceError`. Those errors are deliberately
    not chained to the original ``SpinnakerException``, whose traceback would
    keep the camera alive and stop the SDK from being released.

    Args:
        serial: The camera's serial number, as `discover()` reports it.
        pyspin: A module to use in place of PySpin, for tests.
    """

    def __init__(self, serial: str, *, pyspin: ModuleType | None = None) -> None:
        super().__init__()
        self._serial = str(serial)
        self._system = SpinnakerSystem(pyspin)
        self._pyspin: Any = None
        self._cam: Any = None
        self._model = "Spinnaker camera"
        self._firmware: str | None = None
        self._timestamp_node: str | None = None
        self._timestamp_offset: float | None = None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._serial}>"

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            BACKEND,
            self._model,
            serial_number=self._serial,
            firmware_version=self._firmware,
        )

    @classmethod
    def discover(cls) -> list[DeviceInfo]:
        """List attached Spinnaker cameras without initializing them.

        Raises:
            ImportError: PySpin isn't installed.
        """
        return SpinnakerSystem().discover()

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

    @synchronized
    def measure_timestamp_offset(self) -> float | None:
        """Measure `timestamp_offset` again, e.g. after the host clock jumps.

        Returns:
            The new offset.
        """
        self._measure_timestamp_offset()
        return self._timestamp_offset

    # --- Lifecycle -------------------------------------------------------------

    def _open(self) -> None:
        self._system.open()
        try:
            self._pyspin = self._system._module
            self._cam = self._system._camera(self._serial)
            if self._cam is None:
                raise DeviceNotFound(self._not_found_message())
            self._call(
                "initialize",
                lambda: self._cam.Init(),
                by_code={self._pyspin.SPINNAKER_ERR_ACCESS_DENIED: DeviceBusy},
            )
            self._model = self._read_string("DeviceModelName") or self._model
            self._firmware = self._read_string("DeviceFirmwareVersion")
            self._configure()
            self._call("begin acquisition", lambda: self._cam.BeginAcquisition())
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
        """Let go of the camera and the system, collecting what went wrong."""
        problems = []
        if self._cam is not None:
            steps = (
                ("end acquisition", self._end_acquisition),
                ("de-initialize", self._deinit),
            )
            for action, step in steps:
                try:
                    step()
                except self._pyspin.SpinnakerException as exc:
                    problems.append(f"{action}: {exc}")
            self._cam = None
        self._timestamp_offset = None
        try:
            self._system.close()
        except DeviceError as exc:
            problems.append(str(exc))
        return problems

    def _end_acquisition(self) -> None:
        if self._cam.IsStreaming():
            self._cam.EndAcquisition()

    def _deinit(self) -> None:
        if self._cam.IsInitialized():
            self._cam.DeInit()

    def _not_found_message(self) -> str:
        found = [info.serial_number for info in self._system.discover()]
        attached = ", ".join(s for s in found if s) or "none"
        return (
            f"no Spinnaker camera with serial number {self._serial}; "
            f"attached: {attached}"
        )

    def _configure(self) -> None:
        # Free-running, keeping only the newest frame: the fastest way to
        # capture (pgs-scan dev; docs/hardware/spinnaker.md, Acquisition).
        self._set_enum("StreamBufferHandlingMode", "NewestOnly", stream=True)
        self._set_enum("AcquisitionMode", "Continuous")
        if self._pyspin.IsReadable(self._node("TriggerMode", "CEnumerationPtr")):
            # A camera left in trigger mode (e.g. by SpinView) would never send
            # a frame.
            self._set_enum("TriggerMode", "Off")
        formats = self._enum_choices("PixelFormat", _PIXEL_FORMATS)
        if formats and self._enum_symbol("PixelFormat") not in formats:
            preferred = "RGB8" if "RGB8" in formats else formats[0]
            logger.info("%r: switching pixel format to %s", self, preferred)
            self._set_enum("PixelFormat", preferred)
        self._timestamp_node = next(
            (n for n in _TIMESTAMP_NODES if self._raw_node(n) is not None), None
        )

    @contextmanager
    def _acquisition_paused(self) -> Iterator[None]:
        self._call("end acquisition", self._end_acquisition)
        try:
            yield
        finally:
            self._call("begin acquisition", lambda: self._cam.BeginAcquisition())

    def _measure_timestamp_offset(self) -> None:
        latch = self._node("TimestampLatch", "CCommandPtr")
        if self._timestamp_node is None or not self._pyspin.IsWritable(latch):
            logger.warning("%r can't latch its timestamp", self)
            self._timestamp_offset = None
            return
        value = self._node(self._timestamp_node, "CIntegerPtr")
        offsets = []
        for _ in range(TIMESTAMP_SAMPLES):
            before = time.time()
            self._call("latch the timestamp", latch.Execute)
            after = time.time()
            device = self._call("read the timestamp", value.GetValue)
            offsets.append((before + after) / 2 - device / 1e9)
        self._timestamp_offset = statistics.median(offsets)
        logger.debug("%r timestamp offset %.6f s", self, self._timestamp_offset)

    # --- Role hooks ------------------------------------------------------------

    def _settings(self) -> Mapping[str, Setting]:
        found = (
            self._float_setting(EXPOSURE_TIME, "ExposureTime", "s", _US_PER_S),
            self._enum_setting("ExposureAuto", _AUTO_MODES),
            self._float_setting(GAIN, "Gain", "dB"),
            self._enum_setting("GainAuto", _AUTO_MODES),
            # Read-only while acquiring, but _set_setting pauses acquisition.
            self._enum_setting(
                "PixelFormat", _PIXEL_FORMATS, name=PIXEL_FORMAT, writable=True
            ),
        )
        return {s.name: s for s in found if s is not None}

    def _set_setting(self, name: str, value: SettingValue) -> None:
        logger.debug("%r: %s = %r", self, name, value)
        if name in (EXPOSURE_TIME, GAIN):
            node_name, scale = (
                ("ExposureTime", _US_PER_S) if name == EXPOSURE_TIME else ("Gain", 1.0)
            )
            node = self._node(node_name, "CFloatPtr")
            # Clamp, so a limit that went through the unit conversion still fits.
            low, high = self._call(
                f"read the limits of {name}", lambda: (node.GetMin(), node.GetMax())
            )
            native = min(max(float(value) * scale, low), high)
            self._call(
                f"set {name}", lambda: node.SetValue(native), kind=CommandRejected
            )
        elif name == PIXEL_FORMAT:
            with self._acquisition_paused():
                self._set_enum("PixelFormat", str(value), kind=CommandRejected)
        else:
            self._set_enum(name, str(value), kind=CommandRejected)

    def _float_setting(
        self, name: str, node_name: str, unit: str, scale: float = 1.0
    ) -> Setting | None:
        node = self._node(node_name, "CFloatPtr")
        if not self._pyspin.IsReadable(node):
            return None
        value, low, high = self._call(
            f"read {node_name}",
            lambda: (node.GetValue(), node.GetMin(), node.GetMax()),
        )
        return Setting(
            name,
            value / scale,
            minimum=low / scale,
            maximum=high / scale,
            unit=unit,
            read_only=not self._pyspin.IsWritable(node),
        )

    def _enum_setting(
        self,
        node_name: str,
        supported: Iterable[str],
        *,
        name: str | None = None,
        writable: bool = False,
    ) -> Setting | None:
        node = self._node(node_name, "CEnumerationPtr")
        if not self._pyspin.IsReadable(node):
            return None
        return Setting(
            name or node_name,
            self._enum_symbol(node_name),
            choices=self._enum_choices(node_name, supported),
            read_only=not (writable or self._pyspin.IsWritable(node)),
        )

    def _capture_frame(self, not_before: datetime | None, timeout: float) -> Frame:
        return self._grab(not_before, timeout)

    def _preview_frame(self) -> npt.NDArray[np.uint8]:
        """The newest frame as 8 bits, subsampled to `PREVIEW_MAX_SIZE`."""
        pixels = self._grab(None, self.capture_timeout).pixels
        if pixels.dtype == np.uint16:
            pixels = pixels >> 8
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
        deadline = time.monotonic() + timeout
        if not_before is not None:
            if self._timestamp_offset is None:
                raise DeviceError(
                    f"{self!r} can't check not_before: it can't latch its timestamp"
                )
            wait = (not_before - datetime.now(UTC)).total_seconds()
            if wait >= timeout:
                raise DeviceTimeout(
                    f"{self!r}: not_before is {wait:.3g} s away, past the "
                    f"{timeout:g} s timeout"
                )
            if wait > 0:
                # No frame from before not_before would do, so don't grab any.
                time.sleep(wait)
        exposure = self._read_float("ExposureTime") / _US_PER_S
        stale = incomplete = 0
        remeasured = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeviceTimeout(
                    f"{self!r}: no usable frame in {timeout:g} s "
                    f"({stale} stale, {incomplete} incomplete)"
                )
            if math.isinf(remaining):
                grab_ms = self._pyspin.EVENT_TIMEOUT_INFINITE
            else:
                grab_ms = max(1, math.ceil(remaining * 1000))
            image = self._call(
                "get a frame", functools.partial(self._cam.GetNextImage, grab_ms)
            )
            try:
                taken = self._call(
                    "read a frame", functools.partial(self._take, image, not_before)
                )
            finally:
                self._call("release a frame", image.Release)
            if isinstance(taken, _Taken):
                metadata = {
                    EXPOSURE_TIME: exposure,
                    GAIN: self._read_optional_float("Gain"),
                    "device_timestamp_ns": taken.device_timestamp_ns,
                    "frame_id": taken.frame_id,
                    "retries": stale + incomplete,
                    "stale_frames": stale,
                    "incomplete_frames": incomplete,
                    "missing_trailer": taken.missing_trailer,
                    "timestamp_remeasured": remeasured,
                }
                return Frame(
                    taken.pixels,
                    taken.pixel_format,
                    taken.timestamp,
                    metadata,
                )
            if taken == "incomplete":
                incomplete += 1
                continue
            stale += 1
            if stale % RETRIES_BEFORE_REMEASURE == 0:
                logger.info("%r: %d stale frames; re-measuring offset", self, stale)
                self._measure_timestamp_offset()
                remeasured = True

    def _take(self, image: Any, not_before: datetime | None) -> "_Taken | str":
        """Copy a frame out of its buffer, or say why it won't do."""
        missing_trailer = False
        if image.IsIncomplete():
            status = image.GetImageStatus()
            # Accepted as complete, as in pgs-scan dev (assumed; hardware doc).
            if status != self._pyspin.SPINNAKER_IMAGE_STATUS_MISSING_TRAILER:
                logger.warning(
                    "%r: incomplete frame (%s)",
                    self,
                    self._pyspin.Image.GetImageStatusDescription(status),
                )
                return "incomplete"
            missing_trailer = True
        device_ns = int(image.GetTimeStamp())
        if self._timestamp_offset is None:
            host_time = time.time()
        else:
            host_time = self._timestamp_offset + device_ns / 1e9
        timestamp = datetime.fromtimestamp(host_time, UTC)
        if not_before is not None and timestamp < not_before:
            logger.debug("%r: dropping a frame from %s", self, timestamp)
            return "stale"
        return _Taken(
            pixels=image.GetNDArray().copy(),
            pixel_format=str(image.GetPixelFormatName()),
            timestamp=timestamp,
            device_timestamp_ns=device_ns,
            frame_id=int(image.GetFrameID()),
            missing_trailer=missing_trailer,
        )

    # --- PySpin access ---------------------------------------------------------

    def _call(
        self,
        action: str,
        fn: Callable[[], T],
        *,
        kind: type[DeviceError] = DeviceError,
        by_code: Mapping[int, type[DeviceError]] | None = None,
    ) -> T:
        """Call into PySpin, turning a ``SpinnakerException`` into a `DeviceError`.

        The new error is raised outside the ``except`` block and isn't chained,
        so nothing keeps the SDK's traceback (and the camera in it) alive.
        """
        try:
            return fn()
        except self._pyspin.SpinnakerException as exc:
            code: int = getattr(exc, "errorcode", 0)
            if code == self._pyspin.SPINNAKER_ERR_TIMEOUT:
                kind = DeviceTimeout
            kind = (by_code or {}).get(code, kind)
            error = kind(f"{self!r} failed to {action}: {exc}")
        del fn
        raise error

    def _raw_node(self, name: str, *, stream: bool = False) -> Any:
        nodemap = self._cam.GetTLStreamNodeMap() if stream else self._cam.GetNodeMap()
        return nodemap.GetNode(name)

    def _node(self, name: str, cast: str, *, stream: bool = False) -> Any:
        node = self._raw_node(name, stream=stream)
        return None if node is None else getattr(self._pyspin, cast)(node)

    def _read_float(self, name: str) -> float:
        node = self._node(name, "CFloatPtr")
        return float(self._call(f"read {name}", node.GetValue))

    def _read_optional_float(self, name: str) -> float | None:
        node = self._node(name, "CFloatPtr")
        return self._read_float(name) if self._pyspin.IsReadable(node) else None

    def _read_string(self, name: str) -> str | None:
        node = self._node(name, "CStringPtr")
        if not self._pyspin.IsReadable(node):
            return None
        return str(self._call(f"read {name}", node.GetValue))

    def _enum_symbol(self, name: str, *, stream: bool = False) -> str:
        node = self._node(name, "CEnumerationPtr", stream=stream)
        entry = self._call(f"read {name}", node.GetCurrentEntry)
        return str(entry.GetSymbolic())

    def _enum_choices(self, name: str, supported: Iterable[str]) -> tuple[str, ...]:
        """The entries of an enumeration that are available and in ``supported``."""
        node = self._node(name, "CEnumerationPtr")
        supported = set(supported)
        symbols = []
        for raw in self._call(f"list {name} entries", node.GetEntries):
            entry = self._pyspin.CEnumEntryPtr(raw)
            if self._pyspin.IsReadable(entry) and entry.GetSymbolic() in supported:
                symbols.append(str(entry.GetSymbolic()))
        return tuple(symbols)

    def _set_enum(
        self,
        name: str,
        symbol: str,
        *,
        stream: bool = False,
        kind: type[DeviceError] = DeviceError,
    ) -> None:
        node = self._node(name, "CEnumerationPtr", stream=stream)
        if not self._pyspin.IsWritable(node):
            raise DeviceError(f"{self!r} can't set {name}: it isn't writable")
        entry = self._pyspin.CEnumEntryPtr(node.GetEntryByName(symbol))
        if not self._pyspin.IsReadable(entry):
            raise DeviceError(f"{self!r} has no {name} {symbol!r}")
        value = entry.GetValue()
        self._call(
            f"set {name} to {symbol}", lambda: node.SetIntValue(value), kind=kind
        )


@dataclass(frozen=True)
class _Taken:
    """A usable frame, copied out of its SDK buffer."""

    pixels: npt.NDArray[Any]
    pixel_format: str
    timestamp: datetime
    device_timestamp_ns: int
    frame_id: int
    missing_trailer: bool


def _require_version(pyspin: Any, system: Any) -> None:
    """Release ``system`` and raise `ImportError` if its SDK is too old."""
    try:
        v = system.GetLibraryVersion()
        version = (v.major, v.minor, v.type, v.build)
    except pyspin.SpinnakerException as exc:
        problem = f"could not read the Spinnaker SDK version: {exc}"
    else:
        if version[:3] >= MIN_SPINNAKER_VERSION:
            return
        problem = (
            f"Spinnaker SDK {'.'.join(map(str, version))} is installed, but "
            f"educelab-devices needs {'.'.join(map(str, MIN_SPINNAKER_VERSION))} "
            f"or newer. Install a newer SDK and its matching PySpin wheel. "
            f"{_INSTALL_DOCS}"
        )
    try:
        system.ReleaseInstance()
    except pyspin.SpinnakerException as exc:
        logger.warning("could not release the Spinnaker system: %s", str(exc))
    raise ImportError(problem, name="PySpin")
