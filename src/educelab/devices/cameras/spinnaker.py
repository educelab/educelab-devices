"""FLIR/Teledyne machine-vision cameras, through the Spinnaker SDK (PySpin).

PySpin is imported on first use, so this module imports without it. See
``docs/hardware/spinnaker.md`` for installing the SDK.
"""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType, TracebackType
from typing import Any, Self

from educelab.devices.device import DeviceInfo
from educelab.devices.errors import DeviceError, NotConnected, import_optional

__all__ = ["PYSPIN_INSTALL_HINT", "SpinnakerSystem", "load_pyspin"]

logger = logging.getLogger(__name__)

BACKEND = "spinnaker"

PYSPIN_INSTALL_HINT = (
    "PySpin is not on PyPI. Install the Spinnaker SDK, then the PySpin wheel from "
    "the same SDK release (Python 3.12 and numpy<2 only). See docs/hardware/"
    "spinnaker.md: https://github.com/educelab/educelab-devices/blob/main/docs/"
    "hardware/spinnaker.md"
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
            ImportError: PySpin isn't installed.
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
