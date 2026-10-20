"""DSLR and mirrorless cameras, driven through libgphoto2.

Error codes, timing, and per-body quirks: docs/hardware/gphoto2-cameras.md.
"""

import io
import logging
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from educelab.devices.cameras._gphoto2 import (
    GP_ERROR,
    GP_ERROR_CAMERA_BUSY,
    GP_ERROR_CAMERA_ERROR,
    GP_ERROR_IO_USB_CLAIM,
    GP_ERROR_IO_USB_FIND,
    GP_ERROR_MODEL_NOT_FOUND,
    GP_ERROR_TIMEOUT,
    FilePath,
    GPhoto2Error,
    LibGPhoto2,
    Library,
    Session,
    Widget,
    WidgetValue,
)
from educelab.devices.cameras.base import (
    APERTURE,
    CANONICAL_UNITS,
    EXPOSURE_TIME,
    ISO,
    CameraFile,
    FileCamera,
)
from educelab.devices.cameras.focus import FocusDrive, FocusLimit
from educelab.devices.device import DeviceInfo, Disconnected
from educelab.devices.errors import (
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    FocusLimitReached,
    NotConnected,
    import_optional,
)
from educelab.devices.settings import Setting, SettingValue

__all__ = ["GPhoto2Camera", "GPhoto2FocusDrive", "focus_settle_s", "shutter_seconds"]

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

# Some bodies drop out of PC mode when the bus is quiet for a while, e.g. while
# a turntable moves between captures. From pgs-scan's spectral prototype
# (docs/hardware/gphoto2-cameras.md#keep-alive).
KEEP_ALIVE_INTERVAL_S = 2.0
KEEP_ALIVE_WAIT_S = 0.01
# Pings that must fail in a row before listeners hear `Disconnected`, so one
# hiccup doesn't count as a lost camera.
KEEP_ALIVE_FAILURES = 3

# Focus drive behaviour seen on a Sony a6300 in usb-dslr-capture-demo
# (origin/ankan); unconfirmed on the Z7 II (docs/hardware/gphoto2-cameras.md#focus-drive).
FOCUS_WIDGET = "manualfocusdrive"
FOCUS_CHUNK_STEPS = 1000
FOCUS_SETTLE_BASE_S = 0.1
FOCUS_SETTLE_PER_STEP_S = 0.0005
FOCUS_SETTLE_MAX_S = 3.0
FOCUS_BUSY_ATTEMPTS = 15
FOCUS_BUSY_WAIT_S = 0.5
FOCUS_LIMIT_RECOVERY_S = 1.0
# drive_to_limit() gives up after this many steps without reaching an end stop.
FOCUS_LIMIT_MAX_STEPS = 1_000_000

# Widgets that turn live view on, by camera family, and spellings of "off" for
# continuous AF. From pgs-scan's spectral prototype
# (docs/hardware/gphoto2-cameras.md#live-view).
_VIEWFINDER_WIDGETS = ("viewfinder", "eosviewfinder", "d1a6")
_CONTINUOUS_AF_WIDGETS = ("continuousaf", "continuous-af", "continuousautofocus")
_OFF_SPELLINGS = frozenset({"off", "0", "disable", "disabled", "false", "no"})

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
        # How to write each setting from the last settings read: the widget and
        # a converter to its native value. Every write validates (and so reads)
        # first, so this is never stale.
        self._writers: dict[str, tuple[str, Callable[[SettingValue], WidgetValue]]] = {}
        # Set while the shutter is released or files are downloading, so the
        # keep-alive thread stays off the bus.
        self._capture_in_flight = threading.Event()
        self._keep_alive_stop = threading.Event()
        self._keep_alive_thread: threading.Thread | None = None
        self._focus: GPhoto2FocusDrive | None = None
        self._focus_checked = False
        self._live_view_tried = False
        # The widget that turned live view on, so close() can turn it off.
        self._live_view: str | None = None

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="gphoto2", model=self._model or "unknown", address=self._port
        )

    @property
    def focus(self) -> "GPhoto2FocusDrive | None":
        """The lens's focus drive, or ``None`` if the camera can't drive focus.

        Available when the camera has a range-type ``manualfocusdrive`` widget
        (Nikon, Sony). It shares this camera's connection: it opens with the
        camera, closes and forgets its step count when the camera closes, and
        closing it leaves the camera open. The same object is returned on every
        connection while the lens can drive focus.

        Raises:
            NotConnected: The camera isn't open, so whether it can drive focus
                isn't known yet.
        """
        with self._lock:
            session = self._require_session()
            if not self._focus_checked:
                try:
                    widget = session.config().get(FOCUS_WIDGET)
                except GPhoto2Error as exc:
                    raise self._error(exc, "reading settings") from exc
                self._focus_checked = True
                if widget is None or widget.kind != "range":
                    self._focus = None
                else:
                    if self._focus is None:
                        self._focus = GPhoto2FocusDrive(self)
                    self._focus._attach(widget)
            return self._focus

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
        self._keep_alive_stop = threading.Event()
        self._keep_alive_thread = threading.Thread(
            target=self._keep_alive,
            args=(self._keep_alive_stop,),
            name=f"{self!r} keep-alive",
            daemon=True,
        )
        self._keep_alive_thread.start()

    def _keep_alive(self, stop: threading.Event) -> None:
        """Ping the camera whenever it has been idle for an interval.

        After `KEEP_ALIVE_FAILURES` failed pings in a row, listeners get one
        `Disconnected` event; a good ping starts the count again.
        """
        failures = 0
        while not stop.wait(KEEP_ALIVE_INTERVAL_S):
            if self._capture_in_flight.is_set():
                continue
            # A call holding the lock is already talking to the camera.
            if not self._lock.acquire(blocking=False):
                continue
            # Emitted after releasing the lock, so listeners may call close().
            lost: Disconnected | None = None
            try:
                if stop.is_set() or self._session is None:
                    return
                event = self._session.wait_for_event(KEEP_ALIVE_WAIT_S)
            except GPhoto2Error as exc:
                failures += 1
                logger.warning("%r keep-alive ping failed: %s", self, exc)
                if failures == KEEP_ALIVE_FAILURES:
                    reason = f"{failures} keep-alive pings failed in a row; last: {exc}"
                    lost = Disconnected(self, reason)
            else:
                failures = 0
                if event.kind == "file_added" and event.path is not None:
                    self._discarded(event.path)
            finally:
                self._lock.release()
            if lost is not None:
                self._emit(lost)

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
        self._focus_checked = False
        if self._focus is not None:
            self._focus._detach()
        self._keep_alive_stop.set()
        thread, self._keep_alive_thread = self._keep_alive_thread, None
        # A listener may close the camera from the keep-alive thread itself.
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        session, self._session = self._session, None
        live_view, self._live_view = self._live_view, None
        self._live_view_tried = False
        if session is None:
            return
        if live_view is not None:
            try:
                session.set_config({live_view: 0})
            except GPhoto2Error as exc:
                logger.warning("%r could not turn live view off: %s", self, exc)
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
        session = self._require_session()
        try:
            widgets = session.config()
        except GPhoto2Error as exc:
            raise self._error(exc, "reading settings") from exc
        settings: dict[str, Setting] = {}
        self._writers = {}
        hidden: set[str] = set()
        for canonical in _CANONICAL:
            found = canonical.read(widgets)
            if found is not None:
                setting, widget, natives = found
                settings[setting.name] = setting
                self._writers[setting.name] = (widget.name, natives.__getitem__)
                hidden.update(canonical.widgets)
        for widget in widgets.values():
            if widget.name in hidden or widget.section in _NOT_SETTINGS:
                continue
            native = _native_setting(widget)
            if native is not None:
                settings[widget.name] = native
                to_native = _toggle_value if widget.kind == "toggle" else _same
                self._writers[widget.name] = (widget.name, to_native)
        return settings

    def _set_setting(self, name: str, value: SettingValue) -> None:
        widget, to_native = self._writers[name]
        native = to_native(value)
        logger.debug("%r %s = %r (%s)", self, name, value, native)
        try:
            self._require_session().set_config({widget: native})
        except GPhoto2Error as exc:
            raise self._error(exc, f"setting {name} to {value!r}") from exc

    def _preview_frame(self) -> npt.NDArray[np.uint8]:
        session = self._require_session()
        if not self._live_view_tried:
            self._live_view_tried = True
            self._start_live_view(session)
        try:
            data = session.capture_preview()
        except GPhoto2Error as exc:
            raise self._error(exc, "grabbing a preview") from exc
        image = import_optional("PIL.Image", extra="gphoto2")
        try:
            with image.open(io.BytesIO(data)) as decoded:
                return np.asarray(decoded.convert("RGB"), dtype=np.uint8)
        except (OSError, ValueError) as exc:
            raise DeviceError(f"{self!r}: could not decode the preview: {exc}") from exc

    def _start_live_view(self, session: Session) -> None:
        """Turn live view on and continuous AF off, in one round trip.

        Best effort: most bodies still return previews without this, so a
        failure is logged rather than raised.
        """
        try:
            widgets = session.config()
        except GPhoto2Error as exc:
            logger.warning("%r could not read settings for live view: %s", self, exc)
            return
        values: dict[str, WidgetValue] = {}
        viewfinder = next((n for n in _VIEWFINDER_WIDGETS if n in widgets), None)
        if viewfinder is not None:
            values[viewfinder] = 1
        af = next((widgets[n] for n in _CONTINUOUS_AF_WIDGETS if n in widgets), None)
        if af is not None:
            off = _off_value(af)
            if off is not None and af.value != off:
                values[af.name] = off
        if not values:
            return
        try:
            session.set_config(values)
        except GPhoto2Error as exc:
            logger.warning("%r could not turn live view on: %s", self, exc)
            return
        self._live_view = viewfinder

    def _capture_files(self) -> list[CameraFile]:
        session = self._require_session()
        self._capture_in_flight.set()
        try:
            for stale in self._drain(session):
                self._discarded(stale)
            try:
                first = session.capture()
            except GPhoto2Error as exc:
                raise self._error(exc, "capturing") from exc
            paths = [first]
            paths += [path for path in self._drain(session) if path != first]
            return [CameraFile(p.name, self._download(session, p)) for p in paths]
        finally:
            self._capture_in_flight.clear()

    def _discarded(self, path: FilePath) -> None:
        logger.warning(
            "%r discarded %s, which arrived after its capture had returned",
            self,
            path.name,
        )

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


class GPhoto2FocusDrive(FocusDrive):
    """The focus drive of a `GPhoto2Camera`, reached through ``camera.focus``.

    It shares the camera's connection and lock rather than opening its own:
    `open()` succeeds only while the camera is open, `close()` leaves the
    camera alone, and closing the camera closes this too. Moves larger than
    `FOCUS_CHUNK_STEPS` go in chunks, each followed by a settle wait.
    """

    def __init__(self, camera: GPhoto2Camera) -> None:
        super().__init__()
        self._camera = camera
        # One lock for both, so focus moves, captures, and keep-alive pings
        # never interleave on the bus.
        self._lock = camera._lock
        self._max_chunk = FOCUS_CHUNK_STEPS

    @property
    def info(self) -> DeviceInfo:
        return self._camera.info

    def __repr__(self) -> str:
        return f"<GPhoto2FocusDrive of {self._camera!r}>"

    def _attach(self, widget: Widget) -> None:
        """Mark the drive open for the camera's session, sized to ``widget``."""
        limit = min(abs(widget.minimum or 0), abs(widget.maximum or 0))
        self._max_chunk = int(limit) if limit >= 1 else FOCUS_CHUNK_STEPS
        self._is_open = True

    def _detach(self) -> None:
        """The camera closed: close the drive and forget the count."""
        self._is_open = False
        self._reset_state()

    def _open(self) -> None:
        if not self._camera.is_open:
            raise NotConnected(f"{self!r}: open the camera first")
        # Re-checks the widget if the camera was reopened since it was last read.
        if self._camera.focus is not self:
            raise DeviceError(f"{self!r}: the camera can no longer drive focus")

    def _close(self) -> None:
        pass

    def _step(self, steps: int) -> None:
        chunk = min(FOCUS_CHUNK_STEPS, self._max_chunk)
        sign = 1 if steps > 0 else -1
        remaining = abs(steps)
        while remaining:
            move = min(chunk, remaining)
            self._move(sign * move)
            remaining -= move

    def _drive_to_limit(self, limit: FocusLimit) -> None:
        sign = 1 if limit == "far" else -1
        moved = 0
        while moved < FOCUS_LIMIT_MAX_STEPS:
            try:
                self._move(sign * self._max_chunk)
            except FocusLimitReached:
                return
            moved += self._max_chunk
        raise DeviceError(
            f"{self!r}: no {limit} limit after {moved} steps; the lens may not "
            "report its end stops"
        )

    def _move(self, steps: int) -> None:
        """One write to the focus widget, retried while the camera is busy."""
        session = self._camera._require_session()
        for attempt in range(1, FOCUS_BUSY_ATTEMPTS + 1):
            try:
                session.set_config({FOCUS_WIDGET: float(steps)})
                break
            except GPhoto2Error as exc:
                if exc.code == GP_ERROR_CAMERA_BUSY and attempt < FOCUS_BUSY_ATTEMPTS:
                    logger.debug("%r busy; retrying", self)
                    _wait(FOCUS_BUSY_WAIT_S)
                    continue
                if exc.code == GP_ERROR_CAMERA_ERROR:
                    self._recover_from_end_stop(session)
                    raise FocusLimitReached("far" if steps > 0 else "near") from exc
                error = self._camera._error(exc, f"driving focus {steps:+d} steps")
                if exc.code == GP_ERROR:
                    error = DeviceError(f"{error}. {_FOCUS_MODE_HINT}")
                raise error from exc
        _wait(focus_settle_s(steps))

    def _recover_from_end_stop(self, session: Session) -> None:
        # The camera queues events about the failed move; reading them and
        # pausing before the next move is what kept the a6300 responsive.
        try:
            for stale in self._camera._drain(session):
                self._camera._discarded(stale)
        except DeviceError as exc:
            logger.debug("%r could not drain events after an end stop: %s", self, exc)
        _wait(FOCUS_LIMIT_RECOVERY_S)


_FOCUS_MODE_HINT = (
    "Nikon bodies refuse focus moves in manual focus: set the lens to A or A/M "
    f"and focusmode to AF-S. See {_DOC}#focus-drive."
)


def focus_settle_s(steps: int) -> float:
    """How long to let the lens settle after moving ``steps``, in seconds."""
    seconds = FOCUS_SETTLE_BASE_S + FOCUS_SETTLE_PER_STEP_S * abs(steps)
    return min(seconds, FOCUS_SETTLE_MAX_S)


def _wait(seconds: float) -> None:
    time.sleep(seconds)


def shutter_seconds(text: str) -> float | None:
    """A shutter speed as libgphoto2 shows it (``"1/250"``, ``"2.5"``,
    ``"0.5s"``), in seconds. ``None`` for anything that isn't a duration, such
    as ``"Bulb"`` or ``"Time"``."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?s?", text.strip())
    if match is None:
        return None
    numerator, denominator = match.groups()
    seconds = float(numerator) / float(denominator or 1)
    return seconds if seconds > 0 else None


def _f_number(text: str) -> float | None:
    match = re.fullmatch(r"(?:f/?)?(\d+(?:\.\d+)?)", text.strip(), re.IGNORECASE)
    return float(match.group(1)) if match else None


def _iso(text: str) -> int | None:
    return int(text) if text.strip().isdigit() else None


@dataclass(frozen=True)
class _Canonical:
    """A canonical setting read from the first of ``widgets`` the camera has."""

    name: str
    widgets: tuple[str, ...]
    parse: Callable[[str], float | int | None]

    def read(
        self, widgets: Mapping[str, Widget]
    ) -> tuple[Setting, Widget, dict[SettingValue, str]] | None:
        """The setting, its widget, and the native text of each choice, or
        ``None`` if the camera has no such widget.

        Choices that aren't numbers are left out. While the camera is at one,
        the setting's value is ``None``, and writing any choice leaves it.
        """
        widget = next((widgets[w] for w in self.widgets if w in widgets), None)
        if widget is None or widget.kind not in ("radio", "menu"):
            return None
        value = self.parse(str(widget.value))
        natives: dict[SettingValue, str] = {}
        for text in widget.choices:
            parsed = self.parse(text)
            if parsed is not None:
                natives.setdefault(parsed, text)
        setting = Setting(
            self.name,
            value,
            choices=tuple(natives),
            unit=CANONICAL_UNITS[self.name],
            read_only=widget.read_only,
        )
        return setting, widget, natives


# Shutter speeds that aren't durations (Bulb, Time) and Auto ISO aren't
# numbers, so they aren't choices; at one, the value is None
# (docs/hardware/gphoto2-cameras.md#settings).
_CANONICAL = (
    _Canonical(EXPOSURE_TIME, ("shutterspeed", "shutterspeed2"), shutter_seconds),
    _Canonical(ISO, ("iso",), _iso),
    _Canonical(APERTURE, ("f-number", "aperture"), _f_number),
)

# Widgets in these sections are commands (drive focus, toggle live view).
_NOT_SETTINGS = frozenset({"actions"})


def _native_setting(widget: Widget) -> Setting | None:
    value = widget.value
    if value is None or widget.kind == "button":
        return None
    name, read_only = widget.name, widget.read_only
    if widget.kind in ("radio", "menu"):
        return Setting(name, str(value), choices=widget.choices, read_only=read_only)
    if widget.kind == "range":
        return Setting(
            name,
            float(value),
            widget.minimum,
            widget.maximum,
            widget.step,
            read_only=read_only,
        )
    if widget.kind == "toggle":
        return Setting(name, bool(value), read_only=read_only)
    if widget.kind == "date":
        return Setting(name, int(value), read_only=read_only)
    return Setting(name, str(value), read_only=read_only)


def _off_value(widget: Widget) -> WidgetValue | None:
    """The value that turns a toggle or choice widget off, if it has one."""
    if widget.kind == "toggle":
        return 0
    return next(
        (c for c in widget.choices if c.strip().lower() in _OFF_SPELLINGS), None
    )


def _toggle_value(value: SettingValue) -> WidgetValue:
    return int(bool(value))


def _same(value: SettingValue) -> WidgetValue:
    return value


def _default_library() -> Library:
    return LibGPhoto2()


class _NotVisibleYet(Exception):
    """No camera answered, which may only mean one is still enumerating."""
