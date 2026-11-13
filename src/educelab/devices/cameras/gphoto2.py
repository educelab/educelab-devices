"""DSLR and mirrorless cameras, driven through libgphoto2.

Error codes, timing, and per-body quirks: docs/hardware/gphoto2-cameras.md.
"""

import dataclasses
import io
import logging
import math
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

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
from educelab.devices.cameras._preview import HostCrop, host_crop, reduction
from educelab.devices.cameras.base import (
    APERTURE,
    CANONICAL_UNITS,
    EXPOSURE_TIME,
    ISO,
    CameraFile,
    FileCamera,
    PreviewFrame,
    PreviewGeometry,
    PreviewRegion,
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

# Widgets that turn live view on, by camera family. From pgs-scan's spectral
# prototype (docs/hardware/gphoto2-cameras.md#live-view).
_VIEWFINDER_WIDGETS = ("viewfinder", "eosviewfinder", "d1a6")

LIVE_VIEW_SETTLE_S = 0.4
"""How long after a live-view size, zoom or centre write previews still show
the old setting (0.2-0.4 s on the Z7 II)."""

# Fractions pick up float error; don't let it choose a looser zoom.
_ZOOM_SLACK = 1e-9
# A smaller live-view size is preferred if it delivers this much of the best.
_NEARLY_BEST = 0.8


@dataclass(frozen=True)
class _LiveView:
    """How a body's live view zooms, for bodies where it was measured
    (docs/hardware/gphoto2-cameras.md, Z7 II zoom geometry).

    Captures and unzoomed live view cover the image area (FX, DX, 5:4, 1:1 or
    16:9), whose size in sensor pixels is the largest ``imagesize`` choice.
    ``liveviewzoomarea`` is the crop's width in sensor pixels, always at
    ``zoom_aspect``, and ``changeafarea`` its centre as ``"<x>x<y>"`` pixels of
    the image area. The camera keeps the crop inside the image area.

    Attributes:
        sizes: Frame width of each ``liveviewsize``; unzoomed frames fit a
            4:3 box that wide.
        zoom_aspect: Height over width of a zoomed crop.
    """

    sizes: Mapping[str, int]
    zoom_aspect: float = 2 / 3

    def unzoomed(self, size: str, area: tuple[int, int]) -> tuple[float, float]:
        """An unzoomed frame's width and height at a ``liveviewsize``."""
        box = self.sizes[size]
        width = min(box, 0.75 * box * area[0] / area[1])
        return width, width * area[1] / area[0]


_LIVE_VIEW = {"Nikon Z7_2": _LiveView({"QVGA": 320, "VGA": 640, "XGA": 1024})}
# Preview sets these, so they're reported read-only on the bodies above.
_PREVIEW_WIDGETS = ("liveviewsize", "liveviewzoomarea")
# What each preview start reads to plan the zoom.
_ZOOM_SOURCES = (*_PREVIEW_WIDGETS, "imagesize")
_WHOLE_SENSOR = (0.0, 0.0, 1.0, 1.0)

_DOC = "docs/hardware/gphoto2-cameras.md"

# Read one at a time at open(), which is quick; the whole tree takes seconds.
_SERIAL_WIDGET = "serialnumber"
_FIRMWARE_WIDGET = "deviceversion"
_VENDOR_WIDGET = "manufacturer"


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
        self._serial_number: str | None = None
        self._firmware_version: str | None = None
        self._vendor: str | None = None
        # How to write each setting from the last settings read: the widget and
        # a converter to its native value. Every write validates (and so reads)
        # first, so this is never stale.
        self._writers: dict[str, tuple[str, Callable[[SettingValue], WidgetValue]]] = {}
        # Which widget each setting comes from, and whether it's canonical, so
        # one setting can be read alone. Built by the first full read.
        self._sources: dict[str, tuple[str, _Canonical | None]] = {}
        # Set while the shutter is released or files are downloading, so the
        # keep-alive thread stays off the bus.
        self._capture_in_flight = threading.Event()
        self._keep_alive_stop = threading.Event()
        self._keep_alive_thread: threading.Thread | None = None
        self._focus: GPhoto2FocusDrive | None = None
        self._focus_checked = False
        # The widget that turned live view on, so stopping can turn it off.
        self._live_view: str | None = None
        self._preview_request: tuple[PreviewRegion, int | None, int | None] | None = (
            None
        )
        self._crop: HostCrop | None = None
        # The live-view image shape the crop was planned for.
        self._planned_for: tuple[int, int] | None = None
        # The part of the sensor live-view images show: (x, y, width, height).
        self._view = _WHOLE_SENSOR
        # Zoom levels on offer, widest last; empty where preview can't zoom.
        self._zoom_levels: tuple[int, ...] = ()
        self._size_choices: tuple[str, ...] = ()
        # The image area captures cover, in sensor pixels.
        self._image_area = (0, 0)
        # The image size a capture would have now, where the body says.
        self._capture_size: tuple[int, int] | None = None
        # Live-view size and zoom as found, and as preview last set them.
        self._found: dict[str, WidgetValue] = {}
        self._set: dict[str, WidgetValue] = {}

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="gphoto2",
            model=self._model or "unknown",
            address=self._port,
            serial_number=self._serial_number,
            firmware_version=self._firmware_version,
            vendor=self._vendor,
        )

    @property
    def focus(self) -> "GPhoto2FocusDrive | None":
        """The lens's focus drive, or ``None`` if the camera can't drive focus.

        Available when the camera has a range-type ``manualfocusdrive`` widget
        (Nikon, Sony). It shares this camera's connection: it opens with the
        camera, and closes and forgets its step count when the camera closes.
        Opened on its own, it opens the camera if needed and closes it again
        only if it opened it. The same object is returned on every connection
        while the lens can drive focus.

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
        self._serial_number = self._identity(session, _SERIAL_WIDGET)
        self._firmware_version = self._identity(session, _FIRMWARE_WIDGET)
        self._vendor = self._identity(session, _VENDOR_WIDGET)
        logger.debug("%r opened: %s", self, self._model)
        self._keep_alive_stop = threading.Event()
        self._keep_alive_thread = threading.Thread(
            target=self._keep_alive,
            args=(self._keep_alive_stop,),
            name=f"{self!r} keep-alive",
            daemon=True,
        )
        self._keep_alive_thread.start()

    def _identity(self, session: Session, widget: str) -> str | None:
        """One identity widget's text, or ``None`` if the camera can't say."""
        started = time.monotonic()
        try:
            value = session.value(widget)
        except GPhoto2Error as exc:
            logger.debug("%r could not read %s: %s", self, widget, exc)
            return None
        logger.debug("%r read %s in %.3f s", self, widget, time.monotonic() - started)
        return str(value).strip() or None if value is not None else None

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
        self._writers = {}
        self._sources = {}
        if self._focus is not None:
            self._focus._detach()
        self._keep_alive_stop.set()
        thread, self._keep_alive_thread = self._keep_alive_thread, None
        # A listener may close the camera from the keep-alive thread itself.
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        session, self._session = self._session, None
        self._live_view = None
        self._preview_request = self._crop = None
        self._zoom_levels = ()
        self._found, self._set = {}, {}
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
        session = self._require_session()
        try:
            widgets = session.config()
        except GPhoto2Error as exc:
            raise self._error(exc, "reading settings") from exc
        settings: dict[str, Setting] = {}
        self._writers = {}
        self._sources = {}
        hidden: set[str] = set()
        for canonical in _CANONICAL:
            widget = canonical.find(widgets)
            setting = None if widget is None else self._convert(widget, canonical)
            if setting is not None:
                settings[setting.name] = setting
                hidden.update(canonical.widgets)
        for widget in widgets.values():
            if widget.name in hidden or widget.section in _NOT_SETTINGS:
                continue
            setting = self._convert(widget, None)
            if setting is not None:
                settings[setting.name] = setting
        return settings

    def _setting(self, name: str) -> Setting:
        # The whole tree takes seconds on the Z7 II; one widget is quick
        # (docs/hardware/gphoto2-cameras.md#settings).
        source = self._sources.get(name)
        if source is not None:
            widget_name, canonical = source
            try:
                widget = self._require_session().widget(widget_name)
            except GPhoto2Error as exc:
                raise self._error(exc, f"reading {name}") from exc
            if widget is not None:
                setting = self._convert(widget, canonical)
                if setting is not None:
                    return setting
        # Not read yet, or its widget is gone: read everything and remap.
        return super()._setting(name)

    def _convert(
        self, widget: Widget, canonical: "_Canonical | None"
    ) -> Setting | None:
        """``widget`` as its canonical or native setting, remembering how to
        read and write it. ``None`` if it isn't a setting."""
        if canonical is not None:
            found = canonical.setting(widget)
            if found is None:
                return None
            setting, natives = found
            to_native: Callable[[SettingValue], WidgetValue] = natives.__getitem__
        else:
            native = _native_setting(widget)
            if native is None:
                return None
            setting = native
            if widget.name in _PREVIEW_WIDGETS and self._model in _LIVE_VIEW:
                setting = dataclasses.replace(setting, read_only=True)
            to_native = _toggle_value if widget.kind == "toggle" else _same
        self._writers[setting.name] = (widget.name, to_native)
        self._sources[setting.name] = (widget.name, canonical)
        return setting

    def _set_setting(self, name: str, value: SettingValue) -> None:
        widget, to_native = self._writers[name]
        native = to_native(value)
        logger.debug("%r %s = %r (%s)", self, name, value, native)
        try:
            self._require_session().set_config({widget: native})
        except GPhoto2Error as exc:
            raise self._error(exc, f"setting {name} to {value!r}") from exc

    def _start_preview(
        self, region: PreviewRegion, max_size: int | None, min_size: int | None
    ) -> PreviewGeometry:
        session = self._require_session()
        if self._preview_request is None:
            self._start_live_view(session)
            if self._zoom_levels:
                # The zoom centre is refused until live view is running.
                self._grab_preview(session)
        self._preview_request = (region, max_size, min_size)
        self._view = self._zoom(session, region, max_size, min_size)
        # The live-view size is only known from an image, so grab one.
        return self._plan_crop(self._grab_preview(session))

    def _stop_preview(self) -> None:
        live_view, self._live_view = self._live_view, None
        self._preview_request = self._crop = None
        self._view = _WHOLE_SENSOR
        session = self._require_session()
        restore = {n: v for n, v in self._found.items() if self._set.get(n) != v}
        self._zoom_levels = ()
        self._found, self._set = {}, {}
        # One at a time: a write of several reads the whole tree first.
        for name, value in restore.items():
            try:
                session.set_config({name: value})
            except GPhoto2Error as exc:
                logger.warning("%r could not restore %s: %s", self, name, exc)
        if live_view is None:
            return
        try:
            session.set_config({live_view: 0})
        except GPhoto2Error as exc:
            raise self._error(exc, "turning live view off") from exc

    def _zoom(
        self,
        session: Session,
        region: PreviewRegion,
        max_size: int | None,
        min_size: int | None,
    ) -> tuple[float, float, float, float]:
        """Point live view at the tightest zoom that contains ``region``, at
        the smallest size that still gives the pixels asked for, and return
        the part of the sensor its images show."""
        profile = _LIVE_VIEW.get(self._model or "")
        if profile is None or not self._zoom_levels:
            return _WHOLE_SENSOR
        area_w, area_h = self._image_area
        wide, tall = region.width * area_w, region.height * area_h
        aspect = profile.zoom_aspect
        # Only a level that holds the whole region: the preview must show
        # what was asked for, even where a tighter zoom would be sharper.
        level = next(
            (
                n
                for n in self._zoom_levels
                if wide <= n + _ZOOM_SLACK and tall <= n * aspect + _ZOOM_SLACK
            ),
            0,
        )
        # The part of the image area a zoomed frame shows, as fractions.
        shown_w = min(level / area_w, 1.0) if level else 1.0
        shown_h = min(level * aspect / area_h, 1.0) if level else 1.0

        def longest(size: str) -> float:
            """Pixels on the region's longest side at a live-view size."""
            if level:
                frame_w = float(profile.sizes[size])
                frame_h = frame_w * aspect
            else:
                frame_w, frame_h = profile.unzoomed(size, self._image_area)
            across = region.width / shown_w * frame_w
            return max(across, region.height / shown_h * frame_h)

        def delivered(size: str) -> float:
            """Pixels left of that side once the host has reduced it."""
            side = longest(size)
            return side / reduction(math.ceil(side), max_size, min_size)

        sizes = sorted(self._size_choices, key=profile.sizes.__getitem__)
        best = max(map(delivered, sizes))
        if min_size is not None:
            # The least data that still gives the floor, where any size does.
            enough = min(float(min_size), best)
        else:
            # Nearly the most the cap allows. The first size past the cap
            # isn't it: the host would halve that one.
            enough = _NEARLY_BEST * best
        size = next(n for n in sizes if delivered(n) >= enough)
        wanted: dict[str, WidgetValue] = {
            "liveviewsize": size,
            "liveviewzoomarea": str(level),
        }
        centre_x = (region.x + region.width / 2) * area_w
        centre_y = (region.y + region.height / 2) * area_h
        if level:
            wanted["changeafarea"] = f"{round(centre_x)}x{round(centre_y)}"
        changed = {n: v for n, v in wanted.items() if self._set.get(n) != v}
        try:
            # The centre last, and again with every zoom change: the camera
            # forgets it, and puts a new zoom on the AF point.
            for name in ("liveviewsize", "liveviewzoomarea"):
                if name in changed:
                    session.set_config({name: changed[name]})
            if level and changed:
                session.set_config({"changeafarea": wanted["changeafarea"]})
            if level and "liveviewzoomarea" in changed:
                # The Z7 II misplaces a centre written while the zoom is still
                # changing, in the smaller image areas; a second write lands.
                session.set_config({"changeafarea": wanted["changeafarea"]})
        except GPhoto2Error as exc:
            raise self._error(exc, "zooming live view") from exc
        self._set.update(wanted)
        if not level:
            self._set.pop("changeafarea", None)
        if changed:
            time.sleep(LIVE_VIEW_SETTLE_S)
        if not level:
            return _WHOLE_SENSOR
        # The camera keeps the crop on the image area.
        x = min(max(centre_x / area_w - shown_w / 2, 0.0), 1.0 - shown_w)
        y = min(max(centre_y / area_h - shown_h / 2, 0.0), 1.0 - shown_h)
        return x, y, shown_w, shown_h

    def _preview_frame(self, not_before: datetime | None) -> PreviewFrame:
        # Previews carry no time, so not_before goes by arrival: ask for the
        # image only once not_before has passed.
        if not_before is not None:
            time.sleep(max((not_before - datetime.now(UTC)).total_seconds(), 0.0))
        pixels = self._grab_preview(self._require_session())
        arrived = datetime.now(UTC)
        crop = self._crop
        if crop is None or pixels.shape[:2] != self._planned_for:
            # A setting changed the live-view size; report the new geometry.
            self._preview = self._plan_crop(pixels)
            crop = self._crop
        assert crop is not None
        return PreviewFrame(
            np.ascontiguousarray(crop.apply(pixels)), "RGB8", arrived, crop.geometry, 8
        )

    def _plan_crop(self, pixels: npt.NDArray[np.uint8]) -> PreviewGeometry:
        """Plan the host crop for live-view images the size of ``pixels``,
        which show the `_view` part of the sensor."""
        assert self._preview_request is not None
        region, max_size, min_size = self._preview_request
        height, width = pixels.shape[:2]
        size = (width, height)
        view_x, view_y, view_w, view_h = self._view
        # The region within the images, cropped, then put back on the sensor.
        x = min(max((region.x - view_x) / view_w, 0.0), 1.0)
        y = min(max((region.y - view_y) / view_h, 0.0), 1.0)
        within = PreviewRegion(
            x,
            y,
            min(region.width / view_w, 1.0 - x),
            min(region.height / view_h, 1.0 - y),
        )
        crop = host_crop(within, max_size, sensor=size, frame=size, min_size=min_size)
        shown = crop.geometry.region
        on_sensor = PreviewRegion(
            view_x + shown.x * view_w,
            view_y + shown.y * view_h,
            shown.width * view_w,
            shown.height * view_h,
        )
        geometry = dataclasses.replace(
            crop.geometry, region=on_sensor, capture_size=self._capture_size
        )
        self._crop = dataclasses.replace(crop, geometry=geometry)
        self._planned_for = (height, width)
        return geometry

    def _grab_preview(self, session: Session) -> npt.NDArray[np.uint8]:
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
        """Turn live view on with the viewfinder widget the camera has.

        Best effort: most bodies still return previews without this, so a
        failure is logged rather than raised.
        """
        # Probed by name: the whole tree takes seconds on the Z7 II.
        wanted: tuple[str, ...] = _VIEWFINDER_WIDGETS
        if self._model in _LIVE_VIEW:
            wanted += (*_ZOOM_SOURCES, "changeafarea")
        try:
            widgets = session.find_widgets(wanted)
            names = frozenset(widgets)
            self._find_zoom(names, widgets)
        except GPhoto2Error as exc:
            logger.warning("%r could not read settings for live view: %s", self, exc)
            return
        viewfinder = next((n for n in _VIEWFINDER_WIDGETS if n in names), None)
        if viewfinder is None:
            return
        try:
            session.set_config({viewfinder: 1})
        except GPhoto2Error as exc:
            logger.warning("%r could not turn live view on: %s", self, exc)
            return
        self._live_view = viewfinder

    def _find_zoom(self, names: frozenset[str], widgets: Mapping[str, Widget]) -> None:
        """Note the image area, zoom levels and sizes of a body whose zoom is
        known, from the widgets found on it."""
        profile = _LIVE_VIEW.get(self._model or "")
        needed = (*_ZOOM_SOURCES, "changeafarea")
        self._capture_size = None
        if profile is None or any(name not in names for name in needed):
            return
        if any(name not in widgets for name in _ZOOM_SOURCES):
            return
        areas = [
            (int(m[1]), int(m[2]))
            for choice in widgets["imagesize"].choices
            if (m := re.fullmatch(r"(\d+)x(\d+)", choice))
        ]
        if not areas:
            return
        # The largest size on offer is the image area itself.
        self._image_area = max(areas)
        selected = re.fullmatch(r"(\d+)x(\d+)", str(widgets["imagesize"].value))
        if selected:
            self._capture_size = (int(selected[1]), int(selected[2]))
        levels = (c for c in widgets["liveviewzoomarea"].choices if c.isdigit())
        self._zoom_levels = tuple(sorted(n for n in map(int, levels) if n))
        sizes = widgets["liveviewsize"].choices
        self._size_choices = tuple(n for n in sizes if n in profile.sizes)
        if not self._size_choices:
            self._zoom_levels = ()
            return
        self._found = {
            name: value
            for name in _PREVIEW_WIDGETS
            if (value := widgets[name].value) is not None
        }
        self._set = dict(self._found)

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
    `open()` opens the camera if that is closed, and `close()` then closes the
    camera too, but only in that case. Closing the camera closes this. Moves
    larger than `FOCUS_CHUNK_STEPS` go in chunks, each followed by a settle
    wait.
    """

    def __init__(self, camera: GPhoto2Camera) -> None:
        super().__init__()
        self._camera = camera
        # One lock for both, so focus moves, captures, and keep-alive pings
        # never interleave on the bus.
        self._lock = camera._lock
        self._max_chunk = FOCUS_CHUNK_STEPS
        self._opened_camera = False

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
        opened = not self._camera.is_open
        if opened:
            self._camera.open()
        try:
            # Re-checks the widget if the camera was reopened since it was read.
            if self._camera.focus is not self:
                raise DeviceError(f"{self!r}: the camera can no longer drive focus")
        except BaseException:
            if opened:
                self._camera.close()
            raise
        self._opened_camera = opened

    def _close(self) -> None:
        if self._opened_camera:
            self._camera.close()

    def _reset_state(self) -> None:
        super()._reset_state()
        self._opened_camera = False

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
    "Nikon bodies refuse focus moves in manual focus, or with a lens that has no "
    "focus motor: set the lens to A or A/M and focusmode to AF-S. "
    f"See {_DOC}#focus-drive."
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

    def find(self, widgets: Mapping[str, Widget]) -> Widget | None:
        """The first of ``widgets`` the camera has, if any."""
        return next((widgets[w] for w in self.widgets if w in widgets), None)

    def setting(self, widget: Widget) -> tuple[Setting, dict[SettingValue, str]] | None:
        """The setting and the native text of each choice, or ``None`` if
        ``widget`` isn't a list of choices.

        Choices that aren't numbers are left out. While the camera is at one,
        the setting's value is ``None``, and writing any choice leaves it.
        """
        if widget.kind not in ("radio", "menu"):
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
        return setting, natives


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


def _toggle_value(value: SettingValue) -> WidgetValue:
    return int(bool(value))


def _same(value: SettingValue) -> WidgetValue:
    return value


def _default_library() -> Library:
    return LibGPhoto2()


class _NotVisibleYet(Exception):
    """No camera answered, which may only mean one is still enumerating."""
