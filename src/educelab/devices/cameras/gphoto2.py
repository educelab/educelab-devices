"""DSLR and mirrorless cameras, driven through libgphoto2.

Error codes, timing, and per-body quirks: docs/hardware/gphoto2-cameras.md.
"""

import logging
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

USB_CLAIMED_HINT = (
    "Another process holds the camera's USB interface. On macOS this is "
    "usually ptpcamerad, which claims every camera it sees: quit apps that use "
    "the camera (Image Capture, Photos), run `killall -9 ptpcamerad`, and open "
    "again straight away. On Linux, look for gvfs-gphoto2-volume-monitor. See "
    "docs/hardware/gphoto2-cameras.md#ptpcamerad."
)


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

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="gphoto2", model=self._model or "unknown", address=self._port
        )

    def __repr__(self) -> str:
        return f"<GPhoto2Camera {self._port or '(not found yet)'}>"

    def _open(self) -> None:
        if self._library is None:
            self._library = LibGPhoto2()
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
            return DeviceBusy(f"{message}. {USB_CLAIMED_HINT}")
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
        raise NotImplementedError("GPhoto2Camera capture is not implemented yet")


class _NotVisibleYet(Exception):
    """No camera answered, which may only mean one is still enumerating."""
