"""``el-devices spinnaker``: list Spinnaker cameras and benchmark their captures."""

import argparse
import sys

from educelab.devices.app import _capture


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "spinnaker",
        help="list Spinnaker cameras and benchmark their captures",
        description="List the attached Spinnaker cameras, then open them all at once "
        "(so they all stream, as on a rig) and capture frames from every camera "
        "together, each no older than the moment it was requested. Reports capture "
        "latency, when exposure started, dropped frames, and the bandwidth each camera "
        "streams.",
    )
    _capture.add_arguments(parser)
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from educelab.devices.cameras.spinnaker import SpinnakerCamera, SpinnakerSystem

    try:
        with SpinnakerSystem() as system:
            print(f"Spinnaker SDK {system.library_version}")
            found = system.discover()
    except ImportError as exc:
        print(f"el-devices: {exc}", file=sys.stderr)
        return 1
    listed = _capture.list_cameras(found)
    serials = args.serial or listed
    if not args.frames or not serials:
        return 0
    return _capture.run_benchmark([SpinnakerCamera(s) for s in serials], args)
