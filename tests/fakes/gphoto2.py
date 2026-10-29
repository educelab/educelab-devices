"""Stand-ins for libgphoto2's cameras, behind the driver's `Library` seam."""

import dataclasses
import functools
import io
from collections import deque
from collections.abc import Callable, Iterable, Mapping

from educelab.devices.cameras._gphoto2 import (
    GP_ERROR_BAD_PARAMETERS,
    GP_ERROR_CAMERA_ERROR,
    GP_ERROR_IO_USB_FIND,
    Event,
    FilePath,
    GPhoto2Error,
    Widget,
    WidgetValue,
)

FOLDER = "/store_00010001/DCIM/100NZ7_2"


def _radio(name, value, choices, section="capturesettings", **extra):
    return Widget(
        name, name, "radio", value, choices=tuple(choices), section=section, **extra
    )


# A Nikon-flavoured config tree. The widget names are libgphoto2's; the choice
# lists are illustrative, not a record of what a Z7 II reports.
NIKON_WIDGETS = (
    _radio(
        "shutterspeed",
        "1/60",
        ["1/8000", "1/1000", "1/60", "1/3", "10/13", "1", "2.5", "30", "Bulb"],
    ),
    _radio("iso", "100", ["64", "100", "200", "400", "25600"], section="imgsettings"),
    _radio("f-number", "f/8", ["f/4", "f/5.6", "f/8", "f/11"]),
    _radio(
        "imagequality",
        "NEF (Raw)",
        ["JPEG Fine", "NEF (Raw)", "NEF+Fine"],
        section="capturesettings",
    ),
    _radio("continuousaf", "On", ["Off", "On"], section="capturesettings"),
    Widget(
        "colortemperature",
        "Color Temperature",
        "range",
        5000.0,
        minimum=2500.0,
        maximum=10000.0,
        step=10.0,
        section="imgsettings",
    ),
    Widget("artist", "Artist", "text", "", section="settings"),
    Widget("serialnumber", "Serial Number", "text", "6001234", True, section="status"),
    Widget(
        "manufacturer",
        "Camera Manufacturer",
        "text",
        "Nikon Corporation",
        True,
        section="status",
    ),
    Widget("viewfinder", "Nikon Viewfinder", "toggle", 0, section="actions"),
    Widget(
        "autofocusdrive", "Drive Nikon DSLR Autofocus", "toggle", 0, section="actions"
    ),
    Widget(
        "manualfocusdrive",
        "Drive Nikon DSLR Manual focus",
        "range",
        0.0,
        minimum=-32767.0,
        maximum=32767.0,
        step=1.0,
        section="actions",
    ),
)


@functools.cache
def jpeg(width=64, height=48):
    """A small JPEG, like a live-view frame."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (40, 120, 200)).save(buffer, "JPEG")
    return buffer.getvalue()


class FakeCamera:
    """One camera as libgphoto2 sees it, usable as a `Session` once opened.

    Each capture stores one file per entry of ``suffixes`` on the camera. The
    first is what `capture()` returns; the rest arrive as ``file_added``
    events, as RAW + JPEG does on a Nikon.

    Use `fail_next()` to make the next calls of a method raise, and set
    ``on_capture`` to run code while the shutter is "open".

    Args:
        model: What libgphoto2 identifies the camera as.
        port: The camera's port, e.g. ``"usb:020,007"``.
        widgets: The config tree, flattened. Defaults to `NIKON_WIDGETS`.
        suffixes: Files each capture produces, by extension.
        preview: Bytes `capture_preview()` returns. Defaults to a small JPEG.
        focus_travel: Steps between the lens's near and far limits.
        focus_position: Where the lens starts, in steps from near.

    Writing ``manualfocusdrive`` moves the lens by that many steps, positive
    toward far. A move that would pass a limit stops there and raises
    ``-113``, as the a6300 did (docs/hardware/gphoto2-cameras.md#focus-drive).
    """

    def __init__(
        self,
        model: str = "Nikon Z7_2",
        port: str = "usb:020,007",
        *,
        widgets: Iterable[Widget] = NIKON_WIDGETS,
        suffixes: Iterable[str] = (".NEF",),
        preview: bytes | None = None,
        focus_travel: int = 5000,
        focus_position: int = 2500,
    ) -> None:
        self._model = model
        self.port = port
        self.widgets = {w.name: w for w in widgets}
        self.suffixes = tuple(suffixes)
        self.preview = jpeg() if preview is None else preview
        self.writes: list[dict[str, WidgetValue]] = []
        self.focus_travel = focus_travel
        self.focus_position = focus_position
        self.focus_moves: list[float] = []
        self.storage: dict[FilePath, bytes] = {}
        self.events: deque[Event] = deque()
        self.is_open = False
        self.opens = 0
        self.calls: list[str] = []
        self.on_capture: Callable[[], None] | None = None
        self._failures: dict[str, deque[int]] = {}
        self._shots = 0

    def fail_next(self, method: str, *codes: int) -> None:
        """Make the next ``len(codes)`` calls of ``method`` raise these codes."""
        self._failures.setdefault(method, deque()).extend(codes)

    def _call(self, method: str) -> None:
        self.calls.append(method)
        failures = self._failures.get(method)
        if failures:
            code = failures.popleft()
            raise GPhoto2Error(code, f"fake {method} failure")

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self._call("close")
        self.is_open = False

    def capture(self) -> FilePath:
        self._call("capture")
        if self.on_capture is not None:
            self.on_capture()
        self._shots += 1
        paths = [
            FilePath(FOLDER, f"DSC_{self._shots:04d}{suffix}")
            for suffix in self.suffixes
        ]
        for path in paths:
            self.storage[path] = f"{path.name} contents".encode()
        self.events.extend(Event("file_added", path=path) for path in paths[1:])
        self.events.append(Event("capture_complete"))
        return paths[0]

    def download(self, path: FilePath) -> bytes:
        self._call("download")
        return self.storage[path]

    def wait_for_event(self, timeout_s: float) -> Event:
        self._call("wait_for_event")
        return self.events.popleft() if self.events else Event("timeout")

    def config(self) -> dict[str, Widget]:
        self._call("config")
        return dict(self.widgets)

    def value(self, name: str) -> WidgetValue | None:
        self._call("value")
        widget = self.widgets.get(name)
        return None if widget is None else widget.value

    def set_config(self, values: Mapping[str, WidgetValue]) -> None:
        self._call("set_config")
        for name, value in values.items():
            if not self._accepts(self.widgets[name], value):
                raise GPhoto2Error(GP_ERROR_BAD_PARAMETERS, "Bad parameters")
        self.writes.append(dict(values))
        for name, value in values.items():
            if name == "manualfocusdrive":
                self._drive_focus(float(value))
            else:
                self.widgets[name] = dataclasses.replace(
                    self.widgets[name], value=value
                )

    def _drive_focus(self, steps: float) -> None:
        self.focus_moves.append(steps)
        target = self.focus_position + int(steps)
        self.focus_position = min(max(target, 0), self.focus_travel)
        if self.focus_position != target:
            raise GPhoto2Error(GP_ERROR_CAMERA_ERROR, "Unspecified camera error")

    @staticmethod
    def _accepts(widget: Widget, value: WidgetValue) -> bool:
        if widget.read_only or widget.kind == "button":
            return False
        if widget.kind in ("radio", "menu"):
            return value in widget.choices
        if widget.kind == "text":
            return isinstance(value, str)
        if isinstance(value, str):
            return False
        if widget.kind == "range":
            assert widget.minimum is not None and widget.maximum is not None
            return widget.minimum <= value <= widget.maximum
        return True

    def capture_preview(self) -> bytes:
        self._call("capture_preview")
        return self.preview


class FakeLibrary:
    """libgphoto2 with ``cameras`` attached.

    ``open_errors`` are raised, in order, by the next calls to `open()`, e.g.
    ``-53`` while another process holds the camera.
    """

    def __init__(self, *cameras: FakeCamera, open_errors: Iterable[int] = ()) -> None:
        self.cameras = list(cameras)
        self.open_errors = deque(open_errors)
        self.open_attempts = 0

    def autodetect(self) -> list[tuple[str, str]]:
        return [(camera.model, camera.port) for camera in self.cameras]

    def open(self, port: str) -> FakeCamera:
        self.open_attempts += 1
        if self.open_errors:
            code = self.open_errors.popleft()
            raise GPhoto2Error(code, f"fake open failure {code}")
        for camera in self.cameras:
            if camera.port == port:
                camera.is_open = True
                camera.opens += 1
                return camera
        raise GPhoto2Error(GP_ERROR_IO_USB_FIND, "Could not find the requested device")
