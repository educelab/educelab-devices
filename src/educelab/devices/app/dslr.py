"""``el-devices dslr``: check a camera driven through libgphoto2."""

import argparse
import sys
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

# Imported where it's used, so other subcommands don't load the driver.
if TYPE_CHECKING:
    from educelab.devices.cameras.gphoto2 import GPhoto2Camera
    from educelab.devices.settings import Setting


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "dslr",
        help="detect a gphoto2 camera, list its settings, capture, and step focus",
        description="Open a camera through libgphoto2, list its settings and "
        "whether it has a focus drive, then run whichever checks are asked for. "
        "Without --port, uses the only camera attached.",
    )
    parser.add_argument("--port", help="libgphoto2 port, e.g. usb:020,007")
    parser.add_argument(
        "--exposure",
        type=float,
        metavar="SECONDS",
        help="set exposure_time to the shutter speed nearest this many seconds",
    )
    parser.add_argument(
        "--capture",
        type=Path,
        metavar="DIR",
        help="capture once and save every file to this existing directory, "
        "under the camera's file names",
    )
    parser.add_argument(
        "--preview", action="store_true", help="grab one live-view frame"
    )
    parser.add_argument(
        "--focus-steps",
        type=int,
        metavar="N",
        help="move focus N steps: positive toward far, negative toward near",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from educelab.devices.cameras import EXPOSURE_TIME
    from educelab.devices.cameras.gphoto2 import GPhoto2Camera

    camera = GPhoto2Camera(args.port)
    try:
        camera.open()
    except ImportError as exc:
        print(f"el-devices: {exc}", file=sys.stderr)
        return 1
    with camera:
        print(f"{camera.info.model} at {camera.info.address}")
        for setting in camera.settings().values():
            print(f"  {_describe(setting)}")
        print(f"  focus drive: {'yes' if camera.focus is not None else 'no'}")

        if args.exposure is not None:
            nearest = camera.setting(EXPOSURE_TIME).nearest(args.exposure)
            camera.set_setting(EXPOSURE_TIME, nearest)
            print(f"exposure_time set to {nearest:.4g} s")
        if args.capture is not None and not _capture(camera, args.capture):
            return 1
        if args.preview:
            height, width = camera.preview_frame().shape[:2]
            print(f"preview: {width}x{height}")
        if args.focus_steps is not None and not _step_focus(camera, args.focus_steps):
            return 1
    return 0


def _describe(setting: "Setting") -> str:
    value = setting.value
    if value is None:
        # e.g. exposure_time with the shutter at Bulb
        text = f"{setting.name} = no number"
    else:
        shown = f"{value:.4g}" if isinstance(value, float) else repr(value)
        text = f"{setting.name} = {shown}"
        if setting.unit:
            text += f" {setting.unit}"
    if setting.read_only:
        text += " (read-only)"
    elif setting.choices is not None:
        text += f" ({len(setting.choices)} choices)"
    elif setting.minimum is not None and setting.maximum is not None:
        text += f" ({setting.minimum:g} to {setting.maximum:g})"
    return text


def _capture(camera: "GPhoto2Camera", directory: Path) -> bool:
    files = camera.capture_files()
    for file in files:
        try:
            path = file.save(directory, PurePath(file.name).stem)
        except FileExistsError as exc:
            print(f"el-devices: {exc}", file=sys.stderr)
            return False
        print(f"saved {path} ({len(file.data)} bytes)")
    return True


def _step_focus(camera: "GPhoto2Camera", steps: int) -> bool:
    from educelab.devices.errors import FocusLimitReached

    focus = camera.focus
    if focus is None:
        print(f"el-devices: {camera.info.model} has no focus drive", file=sys.stderr)
        return False
    try:
        focus.step(steps)
    except FocusLimitReached as exc:
        print(f"focus reached the {exc.limit} limit before moving {steps:+d} steps")
        return True
    print(f"focus moved {steps:+d} steps")
    return True
