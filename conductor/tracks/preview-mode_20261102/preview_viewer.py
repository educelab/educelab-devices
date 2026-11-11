"""Look at a camera's preview: frame it, zoom in, move around, capture.

From the repository root, in an environment with the package, the backend's
SDK and OpenCV (``pip install opencv-python``)::

    python conductor/tracks/preview-mode_20261102/preview_viewer.py gphoto2
    python conductor/tracks/preview-mode_20261102/preview_viewer.py spinnaker --serial 20305122
    python conductor/tracks/preview-mode_20261102/preview_viewer.py gentl --serial 4110091493

Controls, with the window focused:

    scroll, or + and -     zoom in and out, about the pointer
    drag, or w a s d       move the region
    r                      back to the whole sensor
    c                      stop, capture, and resume the preview
    q or Esc               quit

The window never shows more than ``--window`` pixels on its longest side: the
camera (or the driver) reduces a larger region, and a region smaller than that
arrives at full resolution and is enlarged without smoothing. The overlay shows
the region and size the camera reports and the frame rate.
"""

import argparse
import re
import subprocess
import sys
import time

import cv2
import numpy as np

from educelab.devices.cameras import FrameCamera, PreviewRegion

BACKENDS = ("gphoto2", "aravis", "spinnaker", "gentl", "simulated")
TITLE = "preview"
SMALLEST = 0.02
# Input settles this long before the camera is asked for the new region.
SETTLE_S = 0.2


def open_camera(backend: str, serial: str | None):
    if backend == "gphoto2":
        from educelab.devices.cameras.gphoto2 import GPhoto2Camera

        if sys.platform == "darwin":
            # macOS grabs the camera for itself otherwise.
            subprocess.run(
                ["killall", "-9", "ptpcamerad"], capture_output=True, check=False
            )
        return GPhoto2Camera()
    if backend == "simulated":
        from educelab.devices.cameras.simulated import SimulatedCamera

        return SimulatedCamera()
    if backend == "aravis":
        from educelab.devices.cameras.aravis import AravisCamera as Driver
    elif backend == "spinnaker":
        from educelab.devices.cameras.spinnaker import SpinnakerCamera as Driver
    else:
        from educelab.devices.cameras.gentl import GenTLCamera as Driver
    if serial is None:
        found = Driver.discover()
        if not found:
            sys.exit(f"{backend} found no cameras")
        serial = found[0].serial_number
    return Driver(serial)


def to_display(pixels: np.ndarray, pixel_format: str, window: int) -> np.ndarray:
    """An 8-bit BGR image for the window, at most ``window`` pixels a side."""
    if pixels.dtype != np.uint8:
        # Left-aligned data fills 16 bits; otherwise go by the format's depth.
        named = re.search(r"\d+", pixel_format)
        depth = int(named.group()) if named else 16
        if int(pixels.max()) >= 1 << depth:
            depth = 16
        pixels = (pixels >> (depth - 8)).astype(np.uint8)
    if pixels.ndim == 2:
        image = cv2.cvtColor(pixels, cv2.COLOR_GRAY2BGR)
    else:
        image = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
    height, width = image.shape[:2]
    scale = window / max(width, height)
    size = (max(round(width * scale), 1), max(round(height * scale), 1))
    smoothing = cv2.INTER_AREA if scale < 1 else cv2.INTER_NEAREST
    return cv2.resize(image, size, interpolation=smoothing)


def label(image: np.ndarray, lines: list[str]) -> None:
    for row, text in enumerate(lines):
        at = (8, 22 + 22 * row)
        for colour, thickness in (((0, 0, 0), 3), ((255, 255, 255), 1)):
            cv2.putText(
                image, text, at, cv2.FONT_HERSHEY_SIMPLEX, 0.55, colour, thickness
            )


class View:
    """The region asked for, and what the pointer does to it."""

    def __init__(self) -> None:
        self.region = (0.0, 0.0, 1.0, 1.0)
        self.shown = self.region
        self.size = (1, 1)
        self.changed_at: float | None = None
        self.drag_from: tuple[int, int] | None = None
        self.pointer = (0, 0)

    def set(self, x: float, y: float, side: float) -> None:
        side = min(max(side, SMALLEST), 1.0)
        x = min(max(x, 0.0), 1.0 - side)
        y = min(max(y, 0.0), 1.0 - side)
        if (x, y, side, side) != self.region:
            self.region = (x, y, side, side)
            self.changed_at = time.perf_counter()

    def zoom(self, factor: float, at: tuple[float, float] = (0.5, 0.5)) -> None:
        """Zoom about a point given in fractions of the window."""
        x, y, side, _ = self.region
        sx, sy, sw, sh = self.shown
        # The sensor point under the pointer stays under it.
        px, py = sx + at[0] * sw, sy + at[1] * sh
        new = min(max(side * factor, SMALLEST), 1.0)
        self.set(px - (px - x) * new / side, py - (py - y) * new / side, new)

    def move(self, dx: float, dy: float) -> None:
        """Move by fractions of the region's own size."""
        x, y, side, _ = self.region
        self.set(x + dx * side, y + dy * side, side)

    def mouse(self, event: int, px: int, py: int, flags: int, _: object) -> None:
        width, height = self.size
        if event == cv2.EVENT_MOUSEWHEEL:
            if sys.platform == "darwin":
                # OpenCV's Cocoa window passes the scroll amount as y, in
                # points, in place of the pointer's position.
                notches, px, py = py / 40, *self.pointer
            else:
                # Elsewhere it is the signed high half of the flags, 120 a notch.
                delta = (flags >> 16) & 0xFFFF
                notches = (delta - (0x10000 if delta >= 0x8000 else 0)) / 120
            if notches:
                factor = 0.8 ** min(max(notches, -3.0), 3.0)
                self.zoom(factor, (px / width, py / height))
            return
        if event == cv2.EVENT_MOUSEHWHEEL:
            return
        self.pointer = (px, py)
        if event == cv2.EVENT_LBUTTONDOWN:
            self.drag_from = (px, py)
        elif event == cv2.EVENT_LBUTTONUP:
            self.drag_from = None
        elif event == cv2.EVENT_MOUSEMOVE and self.drag_from is not None:
            from_x, from_y = self.drag_from
            self.drag_from = (px, py)
            self.move((from_x - px) / width, (from_y - py) / height)

    def outline(self, image: np.ndarray) -> None:
        """Draw the region asked for on an image that still shows another."""
        if self.region == self.shown or self.changed_at is None:
            return
        height, width = image.shape[:2]
        sx, sy, sw, sh = self.shown
        x, y, side, _ = self.region
        first = (round((x - sx) / sw * width), round((y - sy) / sh * height))
        second = (
            round((x + side - sx) / sw * width),
            round((y + side - sy) / sh * height),
        )
        cv2.rectangle(image, first, second, (0, 220, 255), 1)


def capture(camera, window: int) -> None:
    """Stop the preview, capture, and show or name what came."""
    camera.stop_preview()
    started = time.perf_counter()
    if isinstance(camera, FrameCamera):
        frame = camera.capture_frame()
        image = to_display(frame.pixels, frame.pixel_format, window)
        height, width = frame.pixels.shape[:2]
        what = f"captured {width}x{height} {frame.pixel_format}"
    else:
        files = camera.capture_files()
        image = np.zeros((window * 2 // 3, window, 3), np.uint8)
        what = "captured " + ", ".join(
            f"{file.name} ({len(file.data) / 1e6:.1f} MB)" for file in files
        )
    elapsed = time.perf_counter() - started
    print(f"{what} in {elapsed:.2f} s")
    label(image, [what, f"{elapsed:.2f} s; any key resumes the preview"])
    cv2.imshow(TITLE, image)
    cv2.waitKey(0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("backend", choices=BACKENDS)
    parser.add_argument("--serial", help="which camera (default: the first found)")
    parser.add_argument("--window", type=int, default=1000, metavar="PIXELS")
    parser.add_argument("--pixel-format", help="e.g. Mono16 (GenICam cameras)")
    parser.add_argument("--frames", type=int, help="quit after this many frames")
    args = parser.parse_args()

    view = View()
    cv2.namedWindow(TITLE, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(TITLE, view.mouse)
    with open_camera(args.backend, args.serial) as camera:
        if args.pixel_format:
            camera.set_setting("pixel_format", args.pixel_format)
        geometry = camera.start_preview(max_size=args.window)
        took = 0.0
        times: list[float] = []
        count = 0
        while args.frames is None or count < args.frames:
            settled = view.changed_at is not None and (
                time.perf_counter() - view.changed_at >= SETTLE_S
            )
            if settled and view.drag_from is None:
                view.changed_at = None
                started = time.perf_counter()
                wanted = None if view.region[2] == 1.0 else PreviewRegion(*view.region)
                geometry = camera.start_preview(wanted, max_size=args.window)
                took = time.perf_counter() - started
                print(f"{geometry} in {took:.2f} s")
            frame = camera.preview_frame()
            count += 1
            times = [*times[-29:], time.perf_counter()]
            rate = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 else 0
            shown = frame.geometry.region
            view.shown = (shown.x, shown.y, shown.width, shown.height)
            image = to_display(frame.pixels, frame.pixel_format, args.window)
            view.size = (image.shape[1], image.shape[0])
            view.outline(image)
            height, width = frame.pixels.shape[:2]
            label(
                image,
                [
                    f"{camera!r}",
                    (
                        f"region x {shown.x:.3f} y {shown.y:.3f} "
                        f"w {shown.width:.3f} h {shown.height:.3f}"
                    ),
                    (
                        f"{width}x{height} {frame.pixel_format}, {rate:.1f} "
                        f"frames/s, last change {took:.2f} s"
                    ),
                ],
            )
            cv2.imshow(TITLE, image)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key in (ord("+"), ord("=")):
                view.zoom(0.8)
            elif key in (ord("-"), ord("_")):
                view.zoom(1.25)
            elif key == ord("r"):
                view.set(0.0, 0.0, 1.0)
            elif key in (ord("a"), ord("d")):
                view.move(-0.25 if key == ord("a") else 0.25, 0.0)
            elif key in (ord("w"), ord("s")):
                view.move(0.0, -0.25 if key == ord("w") else 0.25)
            elif key == ord("c"):
                capture(camera, args.window)
                wanted = None if view.region[2] == 1.0 else PreviewRegion(*view.region)
                geometry = camera.start_preview(wanted, max_size=args.window)
        camera.stop_preview()
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
