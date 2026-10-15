"""The thin layer between `GPhoto2Camera` and python-gphoto2.

The driver talks to libgphoto2 only through `Library` and `Session`, so its
tests can swap in a fake. `LibGPhoto2` is the real implementation; it is the
only code that imports the SDK.
"""

import contextlib
import logging
import sys
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal, Protocol

from educelab.devices.errors import import_optional

__all__ = [
    "GP_ERROR",
    "GP_ERROR_BAD_PARAMETERS",
    "GP_ERROR_CAMERA_BUSY",
    "GP_ERROR_CAMERA_ERROR",
    "GP_ERROR_IO_USB_CLAIM",
    "GP_ERROR_IO_USB_FIND",
    "GP_ERROR_MODEL_NOT_FOUND",
    "GP_ERROR_TIMEOUT",
    "LIBGPHOTO2_LOGGER",
    "Event",
    "FilePath",
    "GPhoto2Error",
    "LibGPhoto2",
    "Library",
    "LogBridge",
    "Session",
    "Widget",
    "WidgetValue",
    "import_sdk",
]

# libgphoto2 result codes the driver tells apart (gphoto2-port-result.h and
# gphoto2-result.h). What each means on real bodies: docs/hardware/gphoto2-cameras.md.
GP_ERROR = -1
GP_ERROR_BAD_PARAMETERS = -2
GP_ERROR_TIMEOUT = -10
GP_ERROR_IO_USB_FIND = -52
GP_ERROR_IO_USB_CLAIM = -53
GP_ERROR_MODEL_NOT_FOUND = -105
GP_ERROR_CAMERA_BUSY = -110
GP_ERROR_CAMERA_ERROR = -113


class GPhoto2Error(Exception):
    """A libgphoto2 call failed.

    Attributes:
        code: libgphoto2's (negative) result code.
        message: libgphoto2's text for the code.
    """

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


WidgetKind = Literal["text", "range", "toggle", "radio", "menu", "date", "button"]
WidgetValue = str | int | float


@dataclass(frozen=True)
class Widget:
    """One entry in a camera's config tree, as read.

    Attributes:
        name: The widget's name, e.g. ``"shutterspeed"``. Unique in the tree.
        label: Its human-readable label, e.g. ``"Shutter Speed"``.
        kind: The widget type.
        value: Current value: text for text, radio, and menu widgets, a float
            for ranges, and an int for toggles and dates. ``None`` for buttons.
        read_only: Whether the camera refuses changes.
        choices: Allowed values of a radio or menu widget.
        minimum: Lower bound of a range widget.
        maximum: Upper bound of a range widget.
        step: Increment of a range widget.
    """

    name: str
    label: str
    kind: WidgetKind
    value: WidgetValue | None
    read_only: bool = False
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None


@dataclass(frozen=True)
class FilePath:
    """Where a file is on the camera's storage."""

    folder: str
    name: str


EventKind = Literal[
    "timeout",
    "unknown",
    "file_added",
    "file_changed",
    "folder_added",
    "capture_complete",
]


@dataclass(frozen=True)
class Event:
    """Something the camera reported in `Session.wait_for_event()`.

    Attributes:
        kind: What happened. ``"timeout"`` means nothing did.
        path: The file or folder, for the ``file_*`` and ``folder_added`` kinds.
        detail: libgphoto2's description, for ``"unknown"`` events.
    """

    kind: EventKind
    path: FilePath | None = None
    detail: str | None = None


class Session(Protocol):
    """An open connection to one camera. Every method may raise `GPhoto2Error`."""

    @property
    def model(self) -> str:
        """The camera model libgphoto2 identified, e.g. ``"Nikon Z 7II"``."""
        ...

    def close(self) -> None:
        """End the session and release the USB interface."""
        ...

    def capture(self) -> FilePath:
        """Release the shutter and wait for the first file it produces."""
        ...

    def download(self, path: FilePath) -> bytes:
        """Read a file from the camera's storage."""
        ...

    def wait_for_event(self, timeout_s: float) -> Event:
        """Wait up to ``timeout_s`` seconds for the camera's next event."""
        ...

    def config(self) -> dict[str, Widget]:
        """Read the whole config tree, flattened and keyed by widget name."""
        ...

    def set_config(self, values: Mapping[str, WidgetValue]) -> None:
        """Change several widgets in one round trip."""
        ...

    def capture_preview(self) -> bytes:
        """Grab one live-view image, JPEG-encoded on most bodies."""
        ...


class Library(Protocol):
    """Finds and opens cameras. Every method may raise `GPhoto2Error`."""

    def autodetect(self) -> list[tuple[str, str]]:
        """List attached cameras as ``(model, port)`` pairs, e.g.
        ``("Nikon Z 7II", "usb:020,007")``, without opening them."""
        ...

    def open(self, port: str) -> Session:
        """Open the camera at ``port``, as listed by `autodetect()`."""
        ...


LIBGPHOTO2_LOGGER = "educelab.devices.cameras.gphoto2.libgphoto2"
"""Logger that receives libgphoto2's own log messages."""


def import_sdk() -> ModuleType:
    """Import python-gphoto2 and bridge libgphoto2's logging into Python's.

    Raises:
        ImportError: python-gphoto2 isn't installed (or can't be on Windows).
    """
    hint = None
    if sys.platform == "win32":
        hint = "libgphoto2 isn't available on Windows; use macOS or Linux."
    gp = import_optional("gphoto2", extra="gphoto2", hint=hint)
    _log_bridge.install(gp)
    return gp


class LogBridge:
    """Forwards libgphoto2's log messages to `LIBGPHOTO2_LOGGER`.

    libgphoto2 often reports a failure to Python as a bare ``[-1] Unspecified
    error`` and logs the real reason, so these messages matter. Its errors
    become warnings; its verbose and debug output become info and debug.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Dropping python-gphoto2's handle would uninstall the callback.
        self._handle: Any = None

    def install(self, gp: Any) -> None:
        """Register the callback with libgphoto2. Later calls do nothing."""
        with self._lock:
            if self._handle is not None:
                return
            levels = {
                gp.GP_LOG_ERROR: logging.WARNING,
                gp.GP_LOG_VERBOSE: logging.INFO,
                gp.GP_LOG_DEBUG: logging.DEBUG,
            }
            logger = logging.getLogger(LIBGPHOTO2_LOGGER)

            def forward(level: int, domain: str, message: str, data: object) -> None:
                python_level = levels.get(level, logging.DEBUG)
                if logger.isEnabledFor(python_level):
                    logger.log(python_level, "%s: %s", domain, message)

            # GP_LOG_DATA (raw USB bytes) is left out; it floods the log.
            self._handle = gp.gp_log_add_func(gp.GP_LOG_DEBUG, forward, None)


_log_bridge = LogBridge()


@contextlib.contextmanager
def _translated(gp: Any) -> Iterator[None]:
    """Re-raise python-gphoto2's errors as `GPhoto2Error`."""
    try:
        yield
    except gp.GPhoto2Error as exc:
        raise GPhoto2Error(exc.code, exc.string) from exc


class LibGPhoto2:
    """`Library` over python-gphoto2.

    Raises:
        ImportError: python-gphoto2 isn't installed.
    """

    def __init__(self) -> None:
        self._gp = import_sdk()

    def autodetect(self) -> list[tuple[str, str]]:
        with _translated(self._gp):
            return [(model, port) for model, port in self._gp.Camera.autodetect()]

    def open(self, port: str) -> Session:
        gp = self._gp
        with _translated(gp):
            camera = gp.Camera()
            ports = gp.PortInfoList()
            ports.load()
            camera.set_port_info(ports[ports.lookup_path(port)])
            camera.init()
            try:
                return _LibSession(gp, camera)
            except BaseException:
                camera.exit()
                raise


class _LibSession:
    """`Session` over a python-gphoto2 ``Camera``."""

    def __init__(self, gp: Any, camera: Any) -> None:
        self._gp = gp
        self._camera = camera
        with _translated(gp):
            self._model: str = camera.get_abilities().model

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        with _translated(self._gp):
            self._camera.exit()

    def capture(self) -> FilePath:
        with _translated(self._gp):
            path = self._camera.capture(self._gp.GP_CAPTURE_IMAGE)
        return FilePath(path.folder, path.name)

    def download(self, path: FilePath) -> bytes:
        gp = self._gp
        with _translated(gp):
            file = self._camera.file_get(path.folder, path.name, gp.GP_FILE_TYPE_NORMAL)
            return bytes(file.get_data_and_size())

    def wait_for_event(self, timeout_s: float) -> Event:
        gp = self._gp
        with _translated(gp):
            kind, data = self._camera.wait_for_event(round(timeout_s * 1000))
        if kind == gp.GP_EVENT_TIMEOUT:
            return Event("timeout")
        if kind == gp.GP_EVENT_CAPTURE_COMPLETE:
            return Event("capture_complete")
        path_kinds: dict[int, EventKind] = {
            gp.GP_EVENT_FILE_ADDED: "file_added",
            gp.GP_EVENT_FOLDER_ADDED: "folder_added",
        }
        # GP_EVENT_FILE_CHANGED only exists in libgphoto2 2.5.27 and newer.
        if hasattr(gp, "GP_EVENT_FILE_CHANGED"):
            path_kinds[gp.GP_EVENT_FILE_CHANGED] = "file_changed"
        if kind in path_kinds:
            return Event(path_kinds[kind], path=FilePath(data.folder, data.name))
        return Event("unknown", detail=str(data))

    def config(self) -> dict[str, Widget]:
        with _translated(self._gp):
            return flatten_config(self._gp, self._camera.get_config())

    def set_config(self, values: Mapping[str, WidgetValue]) -> None:
        with _translated(self._gp):
            config = self._camera.get_config()
            for name, value in values.items():
                config.get_child_by_name(name).set_value(value)
            self._camera.set_config(config)

    def capture_preview(self) -> bytes:
        with _translated(self._gp):
            return bytes(self._camera.capture_preview().get_data_and_size())


def flatten_config(gp: Any, root: Any) -> dict[str, Widget]:
    """Read every value widget under ``root``, keyed by name.

    Sections are walked but not listed. If two widgets share a name, the
    first one found wins, matching libgphoto2's own lookup by name.
    """
    kinds: dict[int, WidgetKind] = {
        gp.GP_WIDGET_TEXT: "text",
        gp.GP_WIDGET_RANGE: "range",
        gp.GP_WIDGET_TOGGLE: "toggle",
        gp.GP_WIDGET_RADIO: "radio",
        gp.GP_WIDGET_MENU: "menu",
        gp.GP_WIDGET_DATE: "date",
        gp.GP_WIDGET_BUTTON: "button",
    }
    widgets: dict[str, Widget] = {}
    pending = [root]
    while pending:
        node = pending.pop(0)
        kind = kinds.get(node.get_type())
        if kind is None:
            pending[0:0] = list(node.get_children())
            continue
        name = node.get_name()
        if name in widgets:
            continue
        choices: tuple[str, ...] = ()
        minimum = maximum = step = None
        if kind in ("radio", "menu"):
            choices = tuple(node.get_choices())
        elif kind == "range":
            minimum, maximum, step = node.get_range()
        widget = Widget(
            name=name,
            label=node.get_label(),
            kind=kind,
            value=None if kind == "button" else node.get_value(),
            read_only=bool(node.get_readonly()),
            choices=choices,
            minimum=minimum,
            maximum=maximum,
            step=step,
        )
        widgets[name] = widget
    return widgets
