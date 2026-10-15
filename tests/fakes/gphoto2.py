"""Stand-ins for libgphoto2's cameras, behind the driver's `Library` seam."""

import dataclasses
from collections import deque
from collections.abc import Callable, Iterable, Mapping

from educelab.devices.cameras._gphoto2 import (
    GP_ERROR_BAD_PARAMETERS,
    GP_ERROR_IO_USB_FIND,
    Event,
    FilePath,
    GPhoto2Error,
    Widget,
    WidgetValue,
)

FOLDER = "/store_00010001/DCIM/100NZ7_2"


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
        widgets: The config tree, flattened.
        suffixes: Files each capture produces, by extension.
        preview: Bytes `capture_preview()` returns.
    """

    def __init__(
        self,
        model: str = "Nikon Z 7II",
        port: str = "usb:020,007",
        *,
        widgets: Iterable[Widget] = (),
        suffixes: Iterable[str] = (".NEF",),
        preview: bytes = b"",
    ) -> None:
        self._model = model
        self.port = port
        self.widgets = {w.name: w for w in widgets}
        self.suffixes = tuple(suffixes)
        self.preview = preview
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

    def set_config(self, values: Mapping[str, WidgetValue]) -> None:
        self._call("set_config")
        for name, value in values.items():
            widget = self.widgets[name]
            if widget.read_only or (widget.choices and value not in widget.choices):
                raise GPhoto2Error(GP_ERROR_BAD_PARAMETERS, "Bad parameters")
            self.widgets[name] = dataclasses.replace(widget, value=value)

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
