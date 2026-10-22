"""``el-devices genicam``: list USB3 Vision cameras through Aravis or a GenTL
producer, and benchmark their captures."""

import argparse
import sys
from typing import TYPE_CHECKING

from educelab.devices.app import _capture

if TYPE_CHECKING:
    from collections.abc import Callable

    from educelab.devices.cameras._genicam import GenICamCamera
    from educelab.devices.device import DeviceInfo


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "genicam",
        help="list USB3 Vision cameras and benchmark their captures",
        description="List the cameras Aravis or the GenTL producers in "
        "GENICAM_GENTL64_PATH can find, then open them all at once (so they all "
        "stream, as on a rig) and capture frames from every camera together, each "
        "no older than the moment it was requested. Reports capture latency, when "
        "exposure started, dropped frames, and the bandwidth each camera streams.",
    )
    parser.add_argument(
        "--backend",
        choices=("aravis", "gentl"),
        required=True,
        help="the library that reaches the cameras",
    )
    parser.add_argument(
        "--producer",
        metavar="CTI",
        help="with --backend gentl, use only this .cti file (default: every "
        "producer in GENICAM_GENTL64_PATH)",
    )
    _capture.add_arguments(parser)
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if args.producer and args.backend != "gentl":
        print("el-devices: --producer needs --backend gentl", file=sys.stderr)
        return 1
    discover, make = _driver(args.backend, args.producer)
    try:
        found = discover()
    except ImportError as exc:
        print(f"el-devices: {exc}", file=sys.stderr)
        return 1
    listed = _capture.list_cameras(found)
    serials = args.serial or listed
    if not args.frames or not serials:
        return 0
    return _capture.run_benchmark([make(s) for s in serials], args)


def _driver(
    backend: str, producer: str | None
) -> "tuple[Callable[[], list[DeviceInfo]], Callable[[str], GenICamCamera]]":
    """The chosen backend's discover function and camera factory."""
    if backend == "aravis":
        from educelab.devices.cameras.aravis import AravisCamera

        return AravisCamera.discover, AravisCamera
    from educelab.devices.cameras.gentl import GenTLCamera

    return (
        lambda: GenTLCamera.discover(producer),
        lambda serial: GenTLCamera(serial, producer=producer),
    )
