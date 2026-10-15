"""DSLR and mirrorless cameras, driven through libgphoto2.

Error codes, timing, and per-body quirks: docs/hardware/gphoto2-cameras.md.
"""

import logging
import sys
import threading
import time
from collections.abc import Mapping

import numpy as np
import numpy.typing as npt

from educelab.devices.cameras._gphoto2 import (
    GP_ERROR_CAMERA_BUSY,
    GP_ERROR_IO_USB_CLAIM,
    GP_ERROR_IO_USB_FIND,
    GP_ERROR_MODEL_NOT_FOUND,
    GP_ERROR_TIMEOUT,
    FilePath,
    GPhoto2Error,
    LibGPhoto2,
    Library,
    Session,
)
from educelab.devices.cameras.base import CameraFile, FileCamera
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import (
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    NotConnected,
)
from educelab.devices.settings import Setting, SettingValue

__all__ = ["GPhoto2Camera"]

logger = logging.getLogger(__name__)

# A camera that was just plugged in or reset is briefly invisible or reported
# as an unknown model while it re-enumerates. Values from pgs-scan's spectral
# prototype (docs/hardware/gphoto2-cameras.md#opening-a-session).
OPEN_ATTEMPTS = 3
OPEN_RETRY_DELAY_S = 0.5
_STILL_ENUMERATING = frozenset({GP_ERROR_MODEL_NOT_FOUND, GP_ERROR_IO_USB_FIND})

# Files after the first of a capture (the JPEG of RAW + JPEG) arrive as events.
# Reading every pending event also keeps the camera's event buffer from filling,
# which drops it out of PC mode. Values from pgs-scan's spectral prototype
# (docs/hardware/gphoto2-cameras.md#capturing).
EVENT_POLL_S = 0.05
DRAIN_LIMIT_S = 2.0
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_S = 1.0

_DOC = "docs/hardware/gphoto2-cameras.md"


def usb_claimed_hint() -> str:
    """What to do about a claimed USB interface (``-53``) on this host."""
    claimed = "Another process holds the camera's USB interface."
    if sys.platform == "darwin":
        return (
            f"{claimed} On macOS this is usually ptpcamerad, which claims every "
            "camera it sees: quit apps that use the camera (Image Capture, "
            "Photos), run `killall -9 ptpcamerad`, and open again straight away. "
            f"See {_DOC}#macos-ptpcamerad."
        )
    if sys.platform.startswith("linux"):
        return (
            f"{claimed} On Linux this is usually gvfs-gphoto2-volume-monitor, "
            "which mounts cameras as they appear: unmount the camera in the file "
            "manager, or stop the monitor. "
            f"See {_DOC}#linux-gvfs-gphoto2-volume-monitor."
        )
    return f"{claimed} Close it and open again."


class GPhoto2Camera(FileCamera):
    """A camera that libgphoto2 can control over USB, such as a Nikon Z7 II.

    Captures return the camera's own files (e.g. NEF, or NEF + JPEG) in
    memory. Needs the ``gphoto2`` extra, which isn't available on Windows.

    Args:
        port: libgphoto2 port of the camera, e.g. ``"usb:020,007"``, as listed
            by `discover()`. USB ports change when the camera is replugged.
            Without one, `open()` uses the only camera attached.
        library: The libgphoto2 layer to use instead of python-gphoto2. For
            tests.
    """

    def __init__(self, port: str | None = None, *, library: Library | None = None):
        super().__init__()
        self._port = port
        self._fixed_port = port is not None
        self._library = library
        self._session: Session | None = None
        self._model: str | None = None
        # Set while the shutter is released or files are downloading, so the
        # keep-alive thread stays off the bus.
        self._capture_in_flight = threading.Event()

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="gphoto2", model=self._model or "unknown", address=self._port
        )

    @classmethod
    def discover(cls) -> list[DeviceInfo]:
        """List the cameras libgphoto2 can see, without opening them.

        Raises:
            ImportError: python-gphoto2 isn't installed.
            DeviceError: libgphoto2 couldn't scan the bus.
        """
        try:
            found = _default_library().autodetect()
        except GPhoto2Error as exc:
            raise DeviceError(f"looking for gphoto2 cameras failed: {exc}") from exc
        return [DeviceInfo("gphoto2", model, port) for model, port in found]

    def __repr__(self) -> str:
        return f"<GPhoto2Camera {self._port or '(not found yet)'}>"

    def _open(self) -> None:
        if self._library is None:
            self._library = _default_library()
        for attempt in range(1, OPEN_ATTEMPTS + 1):
            try:
                session = self._try_open(self._library)
                break
            except _NotVisibleYet as exc:
                if attempt == OPEN_ATTEMPTS:
                    raise DeviceNotFound(
                        f"{self!r}: no camera answered after {OPEN_ATTEMPTS} "
                        f"attempts ({exc}). Check the cable and that the camera "
                        "is on and not asleep."
                    ) from exc
                logger.debug("%r not ready (%s); retrying", self, exc)
                time.sleep(OPEN_RETRY_DELAY_S)
        self._session = session
        self._model = session.model
        logger.debug("%r opened: %s", self, self._model)

    def _try_open(self, library: Library) -> Session:
        """Open a session, raising `_NotVisibleYet` for failures worth a retry."""
        try:
            if self._port is None or not self._fixed_port:
                self._port = self._only_camera(library.autodetect())
            return library.open(self._port)
        except GPhoto2Error as exc:
            if exc.code in _STILL_ENUMERATING:
                raise _NotVisibleYet(str(exc)) from exc
            raise self._error(exc, "opening the camera") from exc

    def _only_camera(self, found: list[tuple[str, str]]) -> str:
        if not found:
            raise _NotVisibleYet("no camera detected on the USB bus")
        if len(found) > 1:
            choices = ", ".join(f"{model} at {port}" for model, port in found)
            raise DeviceError(
                f"several cameras are attached ({choices}); choose one with port="
            )
        return found[0][1]

    def _close(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        try:
            session.close()
        except GPhoto2Error as exc:
            raise self._error(exc, "closing the camera") from exc

    def _error(self, exc: GPhoto2Error, doing: str) -> DeviceError:
        """The `DeviceError` for a libgphoto2 failure while ``doing`` something."""
        message = f"{self!r}: {doing} failed: {exc}"
        if exc.code == GP_ERROR_IO_USB_CLAIM:
            return DeviceBusy(f"{message}. {usb_claimed_hint()}")
        if exc.code == GP_ERROR_CAMERA_BUSY:
            return DeviceBusy(message)
        if exc.code in _STILL_ENUMERATING:
            return DeviceNotFound(message)
        if exc.code == GP_ERROR_TIMEOUT:
            return DeviceTimeout(message)
        return DeviceError(message)

    def _settings(self) -> Mapping[str, Setting]:
        raise NotImplementedError("GPhoto2Camera settings are not implemented yet")

    def _set_setting(self, name: str, value: SettingValue) -> None:
        raise NotImplementedError("GPhoto2Camera settings are not implemented yet")

    def _preview_frame(self) -> npt.NDArray[np.uint8]:
        raise NotImplementedError("GPhoto2Camera live view is not implemented yet")

    def _capture_files(self) -> list[CameraFile]:
        session = self._require_session()
        self._capture_in_flight.set()
        try:
            for stale in self._drain(session):
                logger.warning(
                    "%r discarded %s, which arrived after its capture had returned",
                    self,
                    stale.name,
                )
            try:
                first = session.capture()
            except GPhoto2Error as exc:
                raise self._error(exc, "capturing") from exc
            paths = [first]
            paths += [path for path in self._drain(session) if path != first]
            return [CameraFile(p.name, self._download(session, p)) for p in paths]
        finally:
            self._capture_in_flight.clear()

    def _drain(self, session: Session) -> list[FilePath]:
        """Read events until the camera goes quiet, returning the files added."""
        added: list[FilePath] = []
        deadline = time.monotonic() + DRAIN_LIMIT_S
        while True:
            try:
                event = session.wait_for_event(EVENT_POLL_S)
            except GPhoto2Error as exc:
                raise self._error(exc, "reading camera events") from exc
            if event.kind == "timeout":
                return added
            logger.debug("%r event: %s", self, event)
            path = event.path
            if event.kind == "file_added" and path is not None and path not in added:
                added.append(path)
            if time.monotonic() >= deadline:
                logger.warning(
                    "%r was still sending events after %.1f s; stopped reading them",
                    self,
                    DRAIN_LIMIT_S,
                )
                return added

    def _download(self, session: Session, path: FilePath) -> bytes:
        for _ in range(DOWNLOAD_ATTEMPTS - 1):
            try:
                return session.download(path)
            except GPhoto2Error as exc:
                logger.debug("%r download of %s failed: %s", self, path.name, exc)
                time.sleep(DOWNLOAD_RETRY_DELAY_S)
        try:
            return session.download(path)
        except GPhoto2Error as exc:
            doing = f"downloading {path.name} ({DOWNLOAD_ATTEMPTS} attempts)"
            raise self._error(exc, doing) from exc

    def _require_session(self) -> Session:
        if self._session is None:
            raise NotConnected(f"{self!r} is not open; call open() first")
        return self._session


def _default_library() -> Library:
    return LibGPhoto2()


class _NotVisibleYet(Exception):
    """No camera answered, which may only mean one is still enumerating."""
