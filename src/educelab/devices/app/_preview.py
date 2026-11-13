"""Preview options shared by the camera subcommands."""

import argparse
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from educelab.devices.cameras import Camera


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add ``--preview``, ``--region``, ``--max-size`` and ``--min-size``."""
    parser.add_argument(
        "--preview",
        type=positive_int,
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
        type=positive_int,
        metavar="PIXELS",
        help="with --preview, the longest side of the preview images (default: "
        "every sensor pixel of the region)",
    )
    parser.add_argument(
        "--min-size",
        type=positive_int,
        metavar="PIXELS",
        help="with --preview, the fewest pixels wanted on the longest side: the "
        "camera reduces no further than that",
    )


def positive_int(text: str) -> int:
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


def problem(args: argparse.Namespace) -> str | None:
    """What's wrong with the preview options, checked before any camera work,
    or ``None``."""
    if not args.preview:
        return None
    from educelab.devices.cameras import PreviewRegion

    try:
        PreviewRegion.of(args.region)
    except ValueError as exc:
        return str(exc)
    low, high = args.min_size, args.max_size
    if low is not None and high is not None and low > high:
        return f"--min-size {low} is above --max-size {high}"
    return None


def run(camera: "Camera", args: argparse.Namespace) -> int:
    """Preview ``args.preview`` frames from ``camera`` and print what came.

    Returns the exit status: 1 if the camera refused the preview options.

    Raises:
        DeviceError: The camera couldn't preview.
    """
    started = time.perf_counter()
    try:
        camera.start_preview(args.region, args.max_size, min_size=args.min_size)
    except ValueError as exc:
        print(f"el-devices: {exc}", file=sys.stderr)
        return 1
    ready = time.perf_counter()
    try:
        frame = camera.preview_frame()
        for _ in range(args.preview - 1):
            frame = camera.preview_frame()
        elapsed = time.perf_counter() - ready
    finally:
        camera.stop_preview()
    # The last frame's, since a driver may replan as the images change size.
    geometry = frame.geometry
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
    return 0
