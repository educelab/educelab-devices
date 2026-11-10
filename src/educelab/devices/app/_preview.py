"""Preview options shared by the camera subcommands."""

import argparse
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from educelab.devices.cameras import Camera


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add ``--preview``, ``--region`` and ``--max-size``."""
    parser.add_argument(
        "--preview",
        type=_frames,
        nargs="?",
        const=1,
        default=0,
        metavar="FRAMES",
        help="preview this many frames (1 if no number is given) and report the "
        "region and size the camera applied, and the frame rate",
    )
    parser.add_argument(
        "--region",
        type=_region,
        metavar="X,Y,W,H",
        help="with --preview, the part of the sensor to show, in fractions of it, "
        "e.g. 0.4,0.4,0.2,0.2 (default: all of it)",
    )
    parser.add_argument(
        "--max-size",
        type=_frames,
        metavar="PIXELS",
        help="with --preview, the longest side of the preview images (default: "
        "every sensor pixel of the region)",
    )


def _frames(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, not {value}")
    return value


def _region(text: str) -> tuple[float, float, float, float]:
    try:
        x, y, width, height = (float(part) for part in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected four numbers X,Y,W,H, not {text!r}"
        ) from None
    return x, y, width, height


def run(camera: "Camera", args: argparse.Namespace) -> None:
    """Preview ``args.preview`` frames from ``camera`` and print what came.

    Raises:
        ValueError: ``--region`` lies outside the sensor.
        DeviceError: The camera couldn't preview.
    """
    started = time.perf_counter()
    geometry = camera.start_preview(args.region, args.max_size)
    ready = time.perf_counter()
    try:
        frame = camera.preview_frame()
        for _ in range(args.preview - 1):
            frame = camera.preview_frame()
        elapsed = time.perf_counter() - ready
    finally:
        camera.stop_preview()
    region = geometry.region
    print(
        f"preview: {geometry.width}x{geometry.height} {frame.pixel_format}, region "
        f"{region.x:.4g},{region.y:.4g},{region.width:.4g},{region.height:.4g}"
    )
    print(
        f"  started in {ready - started:.2f} s; {args.preview} frame(s) in "
        f"{elapsed:.2f} s ({args.preview / elapsed:.1f} frames/s); stopped in "
        f"{time.perf_counter() - ready - elapsed:.2f} s"
    )
