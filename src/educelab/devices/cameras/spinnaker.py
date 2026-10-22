"""FLIR/Teledyne machine-vision cameras, through the Spinnaker SDK (PySpin).

PySpin is imported on first use, so this module imports without it. See
``docs/hardware/spinnaker.md`` for installing the SDK.
"""

import functools
import logging
import math
import threading

# Not used here, but tests patch spinnaker.time.time, as they did when the
# capture logic lived in this module.
import time  # noqa: F401
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from types import ModuleType, TracebackType
from typing import Any, Self, TypeVar

import numpy.typing as npt

from educelab.devices.cameras._genicam import (
    _RETRIGGER_TURNAROUNDS,  # noqa: F401 (tests read it from here)
    PREVIEW_MAX_SIZE,
    RETRIES_BEFORE_REMEASURE,
    THROUGHPUT_LIMIT,
    TIMESTAMP_SAMPLES,
    TRIGGER,
    BufferStatus,
    GenICamCamera,
)
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import (
    DeviceBusy,
    DeviceError,
    DeviceTimeout,
    NotConnected,
    import_optional,
)

__all__ = [
    "MIN_SPINNAKER_VERSION",
    "PREVIEW_MAX_SIZE",
    "PYSPIN_INSTALL_HINT",
    "RETRIES_BEFORE_REMEASURE",
    "THROUGHPUT_LIMIT",
    "TIMESTAMP_SAMPLES",
    "TRIGGER",
    "SpinnakerCamera",
    "SpinnakerSystem",
    "load_pyspin",
]

logger = logging.getLogger(__name__)

BACKEND = "spinnaker"

T = TypeVar("T")


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


class SpinnakerCamera(GenICamCamera):
    """One FLIR/Teledyne camera, addressed by serial number.

    ``open()`` initializes the camera, measures the offset between its
    timestamp counter and the host clock, and starts free-running acquisition
    that keeps only the newest frame, which is the fastest way to capture.
    ``close()`` stops acquiring and de-initializes it. Changing
    ``pixel_format`` or ``trigger`` pauses acquisition briefly, since cameras
    only accept them while stopped. With ``trigger`` set to ``"Software"``,
    each capture fires a trigger (or collects one fired by `trigger()`) and
    returns that frame, so nothing streams between captures.

    A frame's ``timestamp`` is the start of its exposure, in host time. The
    raw device counter, ``device_timestamp_ns`` in its metadata, marks the end.

    Every PySpin error becomes a `DeviceError`. Those errors are deliberately
    not chained to the original ``SpinnakerException``, whose traceback would
    keep the camera alive and stop the SDK from being released.

    The behaviour is shared with every GenICam camera (ADR 0006); this class
    adds PySpin's ``System`` and error handling.

    Args:
        serial: The camera's serial number, as `discover()` reports it.
        pyspin: A module to use in place of PySpin, for tests.
    """

    label = "Spinnaker"
    _no_frame_hint = "another program (e.g. SpinView) may be streaming from this camera"

    def __init__(self, serial: str, *, pyspin: ModuleType | None = None) -> None:
        super().__init__(serial, _PySpinBackend(pyspin))

    @classmethod
    def discover(cls) -> list[DeviceInfo]:
        """List attached Spinnaker cameras without initializing them.

        Raises:
            ImportError: PySpin isn't installed.
        """
        return SpinnakerSystem().discover()


class _PySpinBackend:
    """Opens cameras through a `SpinnakerSystem` handle of their own."""

    name = BACKEND

    def __init__(self, pyspin: ModuleType | None) -> None:
        self._pyspin = pyspin

    def discover(self) -> list[DeviceInfo]:
        return SpinnakerSystem(self._pyspin).discover()

    def open(self, serial: str) -> "_PySpinConnection | None":
        system = SpinnakerSystem(self._pyspin)
        system.open()
        try:
            cam = system._camera(serial)
        except BaseException:
            system.close()
            raise
        if cam is None:
            system.close()
            return None
        conn = _PySpinConnection(system, cam)
        del cam
        try:
            conn._do(
                lambda: conn._cam.Init(),
                by_code={conn._pyspin.SPINNAKER_ERR_ACCESS_DENIED: DeviceBusy},
            )
            # Keep only the newest frame (pgs-scan dev; hardware doc, Acquisition).
            conn._set_stream_enum("StreamBufferHandlingMode", "NewestOnly")
        except BaseException:
            try:
                conn.close()
            except DeviceError as exc:
                logger.warning("while giving up on camera %s: %s", serial, exc)
            raise
        return conn


class _PySpinConnection:
    """A `Connection` onto one initialized PySpin camera.

    Holds the camera pointer and a `SpinnakerSystem` handle until `close()`.
    """

    def __init__(self, system: SpinnakerSystem, cam: Any) -> None:
        self._system = system
        self._pyspin: Any = system._module
        self._cam: Any = cam

    def _do(
        self,
        fn: Callable[[], T],
        *,
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
            kind: type[DeviceError] = DeviceError
            if code == self._pyspin.SPINNAKER_ERR_TIMEOUT:
                kind = DeviceTimeout
            kind = (by_code or {}).get(code, kind)
            error = kind(str(exc))
        del fn
        raise error

    # --- Node map ---------------------------------------------------------------

    def _raw_node(self, name: str, *, stream: bool = False) -> Any:
        cam = self._cam
        nodemap = cam.GetTLStreamNodeMap() if stream else cam.GetNodeMap()
        return nodemap.GetNode(name)

    def _node(self, name: str, cast: str, *, stream: bool = False) -> Any:
        node = self._raw_node(name, stream=stream)
        if node is None:
            raise DeviceError(f"the camera has no {name} node")
        return getattr(self._pyspin, cast)(node)

    def _get(self, name: str, cast: str, getter: Callable[[Any], T]) -> T:
        node = self._node(name, cast)
        return self._do(lambda: getter(node))

    def has(self, name: str) -> bool:
        return self._raw_node(name) is not None

    def is_readable(self, name: str) -> bool:
        return bool(self._pyspin.IsReadable(self._raw_node(name)))

    def is_writable(self, name: str) -> bool:
        return bool(self._pyspin.IsWritable(self._raw_node(name)))

    def get_float(self, name: str) -> float:
        return float(self._get(name, "CFloatPtr", lambda n: n.GetValue()))

    def get_int(self, name: str) -> int:
        return int(self._get(name, "CIntegerPtr", lambda n: n.GetValue()))

    def get_string(self, name: str) -> str:
        return str(self._get(name, "CStringPtr", lambda n: n.GetValue()))

    def get_enum(self, name: str) -> str:
        return self._enum_symbol(name)

    def float_range(self, name: str) -> tuple[float, float]:
        return self._get(name, "CFloatPtr", lambda n: (n.GetMin(), n.GetMax()))

    def int_range(self, name: str) -> tuple[int, int, int]:
        return self._get(
            name, "CIntegerPtr", lambda n: (n.GetMin(), n.GetMax(), n.GetInc())
        )

    def enum_entries(self, name: str) -> tuple[str, ...]:
        node = self._node(name, "CEnumerationPtr")
        symbols = []
        for raw in self._do(node.GetEntries):
            entry = self._pyspin.CEnumEntryPtr(raw)
            if self._pyspin.IsReadable(entry):
                symbols.append(str(entry.GetSymbolic()))
        return tuple(symbols)

    def set_float(self, name: str, value: float) -> None:
        node = self._node(name, "CFloatPtr")
        self._do(lambda: node.SetValue(value))

    def set_int(self, name: str, value: int) -> None:
        node = self._node(name, "CIntegerPtr")
        self._do(lambda: node.SetValue(value))

    def set_enum(self, name: str, symbol: str) -> None:
        self._set_enum(name, symbol)

    def execute(self, name: str) -> None:
        node = self._node(name, "CCommandPtr")
        self._do(node.Execute)

    def _enum_symbol(self, name: str, *, stream: bool = False) -> str:
        node = self._node(name, "CEnumerationPtr", stream=stream)
        entry = self._do(node.GetCurrentEntry)
        return str(entry.GetSymbolic())

    def _set_enum(self, name: str, symbol: str, *, stream: bool = False) -> None:
        node = self._node(name, "CEnumerationPtr", stream=stream)
        entry = self._pyspin.CEnumEntryPtr(node.GetEntryByName(symbol))
        if not self._pyspin.IsReadable(entry):
            raise DeviceError(f"no {name} entry {symbol!r}")
        value = entry.GetValue()
        self._do(lambda: node.SetIntValue(value))

    def _set_stream_enum(self, name: str, symbol: str) -> None:
        node = self._node(name, "CEnumerationPtr", stream=True)
        if not self._pyspin.IsWritable(node):
            raise DeviceError(f"can't set {name}: it isn't writable")
        self._set_enum(name, symbol, stream=True)

    # --- Stream -----------------------------------------------------------------

    def start_acquisition(self) -> None:
        self._do(lambda: self._cam.BeginAcquisition())

    def stop_acquisition(self) -> None:
        self._do(self._end_acquisition)

    def _end_acquisition(self) -> None:
        if self._cam.IsStreaming():
            self._cam.EndAcquisition()

    def grab(self, timeout: float | None) -> "_PySpinBuffer":
        if timeout is None:
            grab_ms = self._pyspin.EVENT_TIMEOUT_INFINITE
        elif timeout <= 0:
            grab_ms = self._pyspin.EVENT_TIMEOUT_NONE
        else:
            grab_ms = max(1, math.ceil(timeout * 1000))
        image = self._do(functools.partial(self._cam.GetNextImage, grab_ms))
        return _PySpinBuffer(self, image)

    def close(self) -> None:
        if self._cam is None:
            return
        problems = []
        steps = (
            ("end acquisition", self._end_acquisition),
            ("de-initialize", self._deinit),
        )
        for action, step in steps:
            try:
                self._do(step)
            except DeviceError as exc:
                problems.append(f"{action}: {exc}")
        self._cam = None
        try:
            self._system.close()
        except DeviceError as exc:
            problems.append(str(exc))
        if problems:
            raise DeviceError("; ".join(problems))

    def _deinit(self) -> None:
        if self._cam.IsInitialized():
            self._cam.DeInit()


class _PySpinBuffer:
    """A `Buffer` over one PySpin image."""

    def __init__(self, conn: _PySpinConnection, image: Any) -> None:
        self._conn = conn
        self._image = image

    def _read(self, fn: Callable[[Any], T]) -> T:
        image = self._image
        return self._conn._do(lambda: fn(image))

    def status(self) -> BufferStatus:
        if not self._read(lambda i: i.IsIncomplete()):
            return BufferStatus.COMPLETE
        status = self._read(lambda i: i.GetImageStatus())
        if status == self._conn._pyspin.SPINNAKER_IMAGE_STATUS_MISSING_TRAILER:
            return BufferStatus.MISSING_TRAILER
        return BufferStatus.INCOMPLETE

    def status_text(self) -> str:
        describe = self._conn._pyspin.Image.GetImageStatusDescription
        return str(self._read(lambda i: describe(i.GetImageStatus())))

    def device_timestamp_ns(self) -> int:
        return int(self._read(lambda i: i.GetTimeStamp()))

    def frame_id(self) -> int:
        return int(self._read(lambda i: i.GetFrameID()))

    def pixel_format(self) -> str:
        return str(self._read(lambda i: i.GetPixelFormatName()))

    def pixels(self) -> npt.NDArray[Any]:
        return self._read(lambda i: i.GetNDArray().copy())

    def release(self) -> None:
        self._read(lambda i: i.Release())


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
