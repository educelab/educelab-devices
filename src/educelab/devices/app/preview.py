"""``el-devices preview``: look at a camera's preview in a window.

A development tool for framing, focus and debugging preview. It needs OpenCV
(the ``[viewer]`` extra), and the subcommand isn't offered without it.
"""

import argparse
import importlib.util
import logging
import re
import sys
import textwrap
import threading
import time
from typing import TYPE_CHECKING, Any

from educelab.devices.app import _macos
from educelab.devices.errors import import_optional

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

    from educelab.devices.cameras import Camera, PreviewFrame

logger = logging.getLogger(__name__)

BACKENDS = ("gphoto2", "aravis", "spinnaker", "gentl", "simulated")
TITLE = "el-devices preview"
SMALLEST = 0.02
# Input settles this long before the camera is asked for the new region.
SETTLE_S = 0.2
# How long the window handles the mouse and keys between redraws.
TICK_MS = 15
# Room left for the window's title bar, in points.
TITLE_BAR = 40
# OpenCV's macOS window reports scrolling in points; elsewhere, 120 a notch.
POINTS_PER_NOTCH = 40
WHEEL_PER_NOTCH = 120

CONTROLS = """\
controls, with the window focused:
  scroll, or + and -   zoom in and out, about the pointer
  drag, or w a s d     move the region
  r                    back to the whole sensor
  c                    stop, capture, and resume the preview
  q or Esc             quit
"""


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    if not has_opencv():
        return
    parser = subparsers.add_parser(
        "preview",
        help="show a camera's preview in a window, to zoom, move and capture",
        description=textwrap.fill(
            "Show a camera's preview in a window. The window never shows more "
            "than --window pixels on its longest side: a larger region is "
            "reduced by the camera or the driver, and a smaller one arrives at "
            "full resolution and is enlarged without smoothing. The overlay "
            "shows the region and size the camera reports and the frame rate.",
            78,
        ),
        epilog=CONTROLS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("backend", choices=BACKENDS)
    parser.add_argument("--serial", help="which camera (default: the first found)")
    parser.add_argument("--port", help="gphoto2: libgphoto2 port, e.g. usb:020,007")
    parser.add_argument("--producer", help="gentl: the .cti file to use")
    parser.add_argument(
        "--window",
        type=int,
        default=1024,
        metavar="PIXELS",
        help="the window's longest side (default: 1024, a Z7 II frame's width)",
    )
    parser.add_argument("--pixel-format", help="set this first, e.g. Mono16")
    parser.add_argument(
        "--frames", type=int, metavar="N", help="quit after this many frames"
    )
    parser.set_defaults(func=run)


def has_opencv() -> bool:
    try:
        return importlib.util.find_spec("cv2") is not None
    except (ImportError, ValueError):
        return False


class View:
    """The square region asked for, in fractions of the sensor, and what
    scrolling and dragging do to it."""

    def __init__(self) -> None:
        self.region = (0.0, 0.0, 1.0)
        # What the image on screen shows, and its size in pixels.
        self.shown = (0.0, 0.0, 1.0, 1.0)
        self.size = (1, 1)
        self.pointer = (0, 0)
        self.dragging = False
        self.changed_at: float | None = None

    def set(self, x: float, y: float, side: float) -> None:
        side = min(max(side, SMALLEST), 1.0)
        region = (min(max(x, 0.0), 1.0 - side), min(max(y, 0.0), 1.0 - side), side)
        if region != self.region:
            self.region = region
            self.changed_at = time.perf_counter()

    def zoom(self, notches: float, at: tuple[int, int] | None = None) -> None:
        """Zoom in by ``notches`` of a scroll wheel, keeping the sensor point
        under the pixel ``at`` (the middle by default) where it is."""
        width, height = self.size
        at_x, at_y = (width / 2, height / 2) if at is None else at
        x, y, side = self.region
        shown_x, shown_y, shown_w, shown_h = self.shown
        point_x = shown_x + at_x / width * shown_w
        point_y = shown_y + at_y / height * shown_h
        new = min(max(side * 0.8 ** min(max(notches, -3.0), 3.0), SMALLEST), 1.0)
        scale = new / side
        self.set(point_x - (point_x - x) * scale, point_y - (point_y - y) * scale, new)

    def move(self, dx: float, dy: float) -> None:
        """Move by fractions of the region's own size."""
        x, y, side = self.region
        self.set(x + dx * side, y + dy * side, side)

    def drag_to(self, pointer: tuple[int, int]) -> None:
        width, height = self.size
        before, self.pointer = self.pointer, pointer
        if self.dragging:
            dx, dy = before[0] - pointer[0], before[1] - pointer[1]
            self.move(dx / width, dy / height)

    def settled(self) -> bool:
        """Whether a change is waiting and input has stopped for long enough."""
        if self.changed_at is None or self.dragging:
            return False
        return time.perf_counter() - self.changed_at >= SETTLE_S

    def request(self) -> tuple[float, float, float, float] | None:
        """The region to ask the camera for; None for the whole sensor."""
        x, y, side = self.region
        return None if side == 1.0 else (x, y, side, side)


def eight_bit(pixels: "npt.NDArray[Any]", pixel_format: str) -> "npt.NDArray[np.uint8]":
    """``pixels`` scaled to 8 bits, for display only."""
    import numpy as np

    if pixels.dtype == np.uint8:
        return pixels
    # Left-aligned data fills 16 bits; otherwise go by the format's depth.
    named = re.search(r"\d+", pixel_format)
    depth = int(named.group()) if named else 16
    if int(pixels.max()) >= 1 << depth:
        depth = 16
    return (pixels >> max(depth - 8, 0)).astype(np.uint8)


def run(args: argparse.Namespace) -> int:
    cv2 = import_optional("cv2", extra="viewer")
    view = View()
    _macos.accept_first_clicks()
    limit = _window_limit()
    cv2.namedWindow(TITLE, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(TITLE, _mouse_handler(cv2, view))
    with _open(args) as camera:
        if args.pixel_format:
            camera.set_setting("pixel_format", args.pixel_format)
        camera.start_preview(max_size=args.window)
        feed = _Feed(camera, args.window)
        feed.start()
        activated = False
        try:
            while args.frames is None or feed.count < args.frames:
                if feed.error is not None:
                    raise feed.error
                frame = feed.frame
                if frame is None:
                    time.sleep(TICK_MS / 1000)
                    continue
                if view.settled():
                    view.changed_at = None
                    logger.debug("asking for %s", view.request())
                    feed.ask(view.request())
                shown = frame.geometry.region
                view.shown = (shown.x, shown.y, shown.width, shown.height)
                pixels = eight_bit(frame.pixels, frame.pixel_format)
                image = _fit(cv2, pixels, args.window, limit)
                view.size = (image.shape[1], image.shape[0])
                if view.changed_at is not None or not feed.caught_up.is_set():
                    _outline(cv2, image, view)
                height, width = frame.pixels.shape[:2]
                _label(
                    cv2,
                    image,
                    f"{camera!r}",
                    f"region x {shown.x:.3f} y {shown.y:.3f} "
                    f"w {shown.width:.3f} h {shown.height:.3f}",
                    f"{width}x{height} {frame.pixel_format}, {feed.rate:.1f} "
                    f"frames/s, last change {feed.took:.2f} s",
                )
                cv2.imshow(TITLE, image)
                if not activated:
                    activated = True
                    _macos.activate()
                key = cv2.waitKey(TICK_MS) & 0xFF
                if key in (ord("q"), 27):
                    break
                if key in (ord("+"), ord("=")):
                    view.zoom(1)
                elif key in (ord("-"), ord("_")):
                    view.zoom(-1)
                elif key == ord("r"):
                    view.set(0.0, 0.0, 1.0)
                elif key in (ord("a"), ord("d")):
                    view.move(-0.25 if key == ord("a") else 0.25, 0.0)
                elif key in (ord("w"), ord("s")):
                    view.move(0.0, -0.25 if key == ord("w") else 0.25)
                elif key == ord("c"):
                    feed.pause()
                    _capture(cv2, camera, args.window, limit)
                    camera.start_preview(view.request(), args.window)
                    feed.resume()
        finally:
            feed.stop()
        camera.stop_preview()
    cv2.destroyAllWindows()
    return 0


class _Feed:
    """Takes preview frames on its own thread.

    The window only handles the mouse and keys between frames, so waiting on
    a camera in the same thread makes dragging lag by seconds.
    """

    def __init__(self, camera: "Camera", max_size: int) -> None:
        self._camera = camera
        self._max_size = max_size
        self._lock = threading.Lock()
        self._asked: list[tuple[float, float, float, float] | None] = []
        self._running = threading.Event()
        self._running.set()
        self._parked = threading.Event()
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="preview", daemon=True)
        # Set once a frame of the last region asked for has arrived.
        self.caught_up = threading.Event()
        self.caught_up.set()
        self.frame: PreviewFrame | None = None
        self.count = 0
        self.rate = 0.0
        self.took = 0.0
        self.error: Exception | None = None

    def start(self) -> None:
        self._thread.start()

    def ask(self, region: tuple[float, float, float, float] | None) -> None:
        """Preview ``region`` from the next frame on; only the latest counts."""
        with self._lock:
            self._asked = [region]
            self.caught_up.clear()

    def pause(self) -> None:
        """Return once the thread has let go of the camera."""
        self._running.clear()
        self._parked.wait()

    def resume(self) -> None:
        self._parked.clear()
        self._running.set()

    def stop(self) -> None:
        self._stopping = True
        self._running.set()
        self._thread.join()

    def _run(self) -> None:
        times: list[float] = []
        try:
            while not self._stopping:
                if not self._running.is_set():
                    self._parked.set()
                    self._running.wait()
                    continue
                with self._lock:
                    asked, self._asked = self._asked, []
                if asked:
                    started = time.perf_counter()
                    geometry = self._camera.start_preview(asked[0], self._max_size)
                    self.took = time.perf_counter() - started
                    print(f"{geometry} in {self.took:.2f} s")
                frame = self._camera.preview_frame()
                times = [*times[-29:], time.perf_counter()]
                with self._lock:
                    self.frame = frame
                    self.count += 1
                    if len(times) > 1:
                        self.rate = (len(times) - 1) / (times[-1] - times[0])
                    if not self._asked:
                        self.caught_up.set()
        except Exception as exc:  # noqa: BLE001
            # Handed to the window's thread, which raises it.
            self.error = exc
        finally:
            self.caught_up.set()
            self._parked.set()


def _open(args: argparse.Namespace) -> "Camera":
    backend = args.backend
    if backend == "gphoto2":
        from educelab.devices.cameras.gphoto2 import GPhoto2Camera

        return GPhoto2Camera(args.port)
    if backend == "simulated":
        from educelab.devices.cameras.simulated import SimulatedCamera

        return SimulatedCamera()
    if backend == "gentl":
        from educelab.devices.cameras.gentl import GenTLCamera

        found = GenTLCamera.discover(args.producer)
        serial = _serial(args, [info.serial_number for info in found])
        return GenTLCamera(serial, producer=args.producer)
    if backend == "aravis":
        from educelab.devices.cameras.aravis import AravisCamera

        found = AravisCamera.discover()
        return AravisCamera(_serial(args, [info.serial_number for info in found]))
    from educelab.devices.cameras.spinnaker import SpinnakerCamera

    found = SpinnakerCamera.discover()
    return SpinnakerCamera(_serial(args, [info.serial_number for info in found]))


def _serial(args: argparse.Namespace, found: list[str | None]) -> str:
    if args.serial:
        return str(args.serial)
    first = next((serial for serial in found if serial), None)
    if first is None:
        sys.exit(f"el-devices: {args.backend} found no cameras")
    return first


def _mouse_handler(cv2: Any, view: View) -> Any:
    def handle(event: int, x: int, y: int, flags: int, _: object) -> None:
        if event != cv2.EVENT_MOUSEMOVE or flags:
            logger.debug("mouse event %d at (%d, %d), flags %#x", event, x, y, flags)
        if event == cv2.EVENT_MOUSEHWHEEL:
            return
        if event == cv2.EVENT_MOUSEWHEEL:
            if sys.platform == "darwin":
                # OpenCV's Cocoa window passes the scroll amount as y, in
                # place of the pointer's position.
                if y:
                    view.zoom(y / POINTS_PER_NOTCH, view.pointer)
            else:
                # Elsewhere it is the signed high half of the flags.
                delta = (flags >> 16) & 0xFFFF
                delta -= 0x10000 if delta >= 0x8000 else 0
                view.zoom(delta / WHEEL_PER_NOTCH, (x, y))
            return
        held = bool(flags & cv2.EVENT_FLAG_LBUTTON)
        if event == cv2.EVENT_LBUTTONUP:
            view.drag_to((x, y))
            held = False
        elif event == cv2.EVENT_LBUTTONDOWN:
            held = True
        elif event != cv2.EVENT_MOUSEMOVE:
            return
        # A move says whether the button is down, so a press or release that
        # never arrives doesn't leave the drag stuck. A drag starts where it
        # is first seen, wherever the pointer was before.
        if held and not view.dragging:
            view.pointer = (x, y)
        if held != view.dragging:
            logger.debug("drag %s at %s, region %s", held, (x, y), view.region)
        view.dragging = held
        view.drag_to((x, y))

    return handle


def _window_limit() -> tuple[int, int] | None:
    """The largest image a window can show whole, where that's known.

    OpenCV's macOS window halves an image that doesn't fit the screen, and
    draws it badly, so the viewer stays within the screen itself.
    """
    screen = _macos.visible_screen_size()
    if screen is None:
        return None
    return screen[0], screen[1] - TITLE_BAR


def _fit(
    cv2: Any,
    pixels: "npt.NDArray[np.uint8]",
    window: int,
    limit: tuple[int, int] | None = None,
) -> Any:
    """A BGR image for the window, ``window`` pixels on its longest side, or
    smaller to stay within ``limit``."""
    code = cv2.COLOR_GRAY2BGR if pixels.ndim == 2 else cv2.COLOR_RGB2BGR
    return _resize(cv2, cv2.cvtColor(pixels, code), window, limit)


def _resize(cv2: Any, image: Any, window: int, limit: tuple[int, int] | None) -> Any:
    height, width = image.shape[:2]
    scale = window / max(width, height)
    if limit is not None:
        scale = min(scale, limit[0] / width, limit[1] / height)
    size = (max(round(width * scale), 1), max(round(height * scale), 1))
    # Enlarged pixels stay sharp, to judge focus by.
    smoothing = cv2.INTER_AREA if scale < 1 else cv2.INTER_NEAREST
    return cv2.resize(image, size, interpolation=smoothing)


def _label(cv2: Any, image: Any, *lines: str) -> None:
    font, size = cv2.FONT_HERSHEY_SIMPLEX, 0.55
    for row, text in enumerate(lines):
        x, y = 8, 22 + 22 * row
        # A dark edge from shifted copies: a thicker stroke would also be
        # wider in OpenCV 5, and no longer line up.
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (1, 1)):
            cv2.putText(image, text, (x + dx, y + dy), font, size, (0, 0, 0), 1)
        cv2.putText(image, text, (x, y), font, size, (255, 255, 255), 1)


def _outline(cv2: Any, image: Any, view: View) -> None:
    """Draw the region asked for on an image that still shows another."""
    if view.changed_at is None:
        return
    height, width = image.shape[:2]
    shown_x, shown_y, shown_w, shown_h = view.shown
    x, y, side = view.region
    corners = [
        (
            round((corner_x - shown_x) / shown_w * width),
            round((corner_y - shown_y) / shown_h * height),
        )
        for corner_x, corner_y in ((x, y), (x + side, y + side))
    ]
    cv2.rectangle(image, corners[0], corners[1], (0, 220, 255), 1)


def _capture(
    cv2: Any, camera: "Camera", window: int, limit: tuple[int, int] | None = None
) -> None:
    """Stop the preview, capture, and show or name what came."""
    import numpy as np

    from educelab.devices.cameras import FileCamera, FrameCamera

    camera.stop_preview()
    started = time.perf_counter()
    if isinstance(camera, FrameCamera):
        frame = camera.capture_frame()
        image = _fit(cv2, eight_bit(frame.pixels, frame.pixel_format), window, limit)
        height, width = frame.pixels.shape[:2]
        what = f"captured {width}x{height} {frame.pixel_format}"
    else:
        assert isinstance(camera, FileCamera)
        files = camera.capture_files()
        what = "captured " + ", ".join(
            f"{file.name} ({len(file.data) / 1e6:.1f} MB)" for file in files
        )
        # A JPEG can be shown; a raw file is only named (ADR 0002).
        decoded = None
        for file in files:
            if file.suffix.lower() in (".jpg", ".jpeg"):
                data = np.frombuffer(file.data, np.uint8)
                decoded = cv2.imdecode(data, cv2.IMREAD_COLOR)
                break
        if decoded is None:
            image = np.zeros((window * 2 // 3, window, 3), np.uint8)
        else:
            image = _resize(cv2, decoded, window, limit)
    elapsed = time.perf_counter() - started
    print(f"{what} in {elapsed:.2f} s")
    _label(cv2, image, what, f"{elapsed:.2f} s; any key resumes the preview")
    cv2.imshow(TITLE, image)
    cv2.waitKey(0)
