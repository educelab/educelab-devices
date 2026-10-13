"""``el-devices simulate``: exercise the simulated devices."""

import argparse
from collections.abc import Callable

from educelab.devices.cameras import (
    EXPOSURE_TIME,
    SimulatedCamera,
    SimulatedFileCamera,
    SimulatedFocusDrive,
)
from educelab.devices.hubs import SimulatedHub
from educelab.devices.lights import ChannelKind, SimulatedLight
from educelab.devices.stages import SimulatedStage


def _light() -> None:
    with SimulatedLight() as light:
        for channel in light.channels:
            if channel.kind is ChannelKind.RGB:
                light.set_color(channel.id, (1.0, 0.5, 0.0))
            else:
                light.set_level(channel.id, 1.0)
        print(f"  levels: {_levels(light)}")
        light.all_off()
        print(f"  after all_off: {_levels(light)}")


def _stage() -> None:
    with SimulatedStage() as stage:
        stage.move_to({"x": 100.0, "y": 50.0})
        print(f"  after move_to: {_format(stage.position())}")
        stage.move_by({"theta": 450.0})
        print(f"  after move_by theta +450: {_format(stage.position())}")


def _camera() -> None:
    with SimulatedCamera() as camera:
        camera.set_setting(EXPOSURE_TIME, 0.005)
        frame = camera.capture_frame()
        print(
            f"  frame: {frame.pixels.shape} {frame.pixel_format}, "
            f"mean {frame.pixels.mean():.1f}"
        )


def _file_camera() -> None:
    with SimulatedFileCamera() as camera:
        for file in camera.capture_files():
            print(f"  file: {file.name}, {len(file.data)} bytes")


def _hub() -> None:
    with SimulatedHub() as hub:
        hub.power_cycle(hub.ports[0], off_time=0.1)
        states = {port: hub.is_powered(port) for port in hub.ports}
        print(f"  powered after power_cycle: {states}")


def _focus() -> None:
    with SimulatedFocusDrive() as drive:
        drive.drive_to_limit("near")
        drive.step(120)
        print(f"  {drive.focus_steps} steps from the {drive.last_limit} limit")


ROLES: dict[str, Callable[[], None]] = {
    "light": _light,
    "stage": _stage,
    "camera": _camera,
    "file-camera": _file_camera,
    "hub": _hub,
    "focus": _focus,
}


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "simulate",
        help="exercise the simulated devices",
        description="Run each simulated device through a few operations. "
        "Useful for checking an install without hardware.",
    )
    parser.add_argument(
        "roles",
        nargs="*",
        type=_role,
        metavar="ROLE",
        help=f"roles to exercise: {', '.join(ROLES)} (default: all)",
    )
    parser.set_defaults(func=run)


def _role(name: str) -> str:
    if name not in ROLES:
        raise argparse.ArgumentTypeError(
            f"unknown role {name!r} (choose from {', '.join(ROLES)})"
        )
    return name


def run(args: argparse.Namespace) -> int:
    for role in args.roles or ROLES:
        print(f"[{role}]")
        ROLES[role]()
    return 0


def _levels(light: SimulatedLight) -> str:
    return ", ".join(
        f"{c.id}={light.color(c.id) if c.kind is ChannelKind.RGB else light.level(c.id)}"
        for c in light.channels
    )


def _format(position: dict[str, float]) -> str:
    return ", ".join(f"{name}={value:g}" for name, value in position.items())
