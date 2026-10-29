"""``el-devices xcarve``: check an X-Carve gantry by hand."""

import argparse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

# Imported where it's used, so other subcommands don't load the driver.
if TYPE_CHECKING:
    from educelab.devices.stages.xcarve import XCarve

# pgs-scan's machine bounds for its X-Carves, in machine coordinates.
DEFAULT_LIMITS = {"x": (-699.0, 0.0), "y": (-699.0, 0.0), "z": (-45.0, -1.0)}


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "xcarve",
        help="check an X-Carve gantry",
        description="Report, home, and move an X-Carve through its X-Controller. "
        "Opening the port resets the controller, so homing is lost on every run "
        "and every action that moves homes first. Ctrl-C stops the machine. "
        "Without --port or --serial-number, uses the one X-Controller attached.",
    )
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--port", help="serial port, e.g. /dev/cu.usbserial-21320")
    where.add_argument("--serial-number", help="USB serial number of the port")
    parser.add_argument(
        "--speed",
        type=float,
        default=10.0,
        metavar="MM_PER_S",
        help="speed for moves, in mm/s (default: 10, slow)",
    )
    for axis, (low, high) in DEFAULT_LIMITS.items():
        parser.add_argument(
            f"--{axis}-limits",
            type=float,
            nargs=2,
            default=(low, high),
            metavar=("MIN", "MAX"),
            help=f"{axis} range in machine coordinates, mm (default: {low:g} {high:g})",
        )
    actions = parser.add_subparsers(
        title="actions", dest="action", metavar="ACTION", required=True
    )

    actions.add_parser(
        "status", help="show firmware, maximum rates, and position, without moving"
    ).set_defaults(func=_status)
    actions.add_parser("home", help="home every axis").set_defaults(func=_home)

    move = actions.add_parser(
        "move",
        help="home, then move to a machine position",
        description="Home, then move to an absolute machine position. Axes not "
        "given stay where homing left them.",
    )
    for axis in DEFAULT_LIMITS:
        move.add_argument(f"--{axis}", type=float, metavar="MM")
    move.set_defaults(func=_move)

    check = actions.add_parser(
        "check",
        help="home, then move each axis off home and back",
        description="Home, then move each axis by --distance toward the middle of "
        "its range and back, checking each position.",
    )
    check.add_argument(
        "--distance",
        type=float,
        default=10.0,
        metavar="MM",
        help="how far to move each axis (default: 10)",
    )
    check.set_defaults(func=_check)


def _stage(args: argparse.Namespace) -> "XCarve":
    from educelab.devices.stages.xcarve import XCarve

    limits = {axis: tuple(getattr(args, f"{axis}_limits")) for axis in DEFAULT_LIMITS}
    return XCarve(
        args.port,
        serial_number=args.serial_number,
        limits=limits,  # type: ignore[arg-type]
        speed=args.speed,
    )


@contextmanager
def _stopping_on_interrupt(stage: "XCarve") -> Iterator[None]:
    """Stop the machine on Ctrl-C, including during homing."""
    try:
        yield
    except KeyboardInterrupt:
        stage.stop()
        print("\nstopped")
        raise


def _run(args: argparse.Namespace, action: Callable[["XCarve"], int]) -> int:
    try:
        stage = _stage(args)
        with stage, _stopping_on_interrupt(stage):
            info = stage.info
            print(
                f"X-Carve at {info.address}, "
                f"serial {info.serial_number or 'unknown'}, "
                f"GRBL {info.firmware_version}"
            )
            return action(stage)
    except ValueError as exc:
        # e.g. a speed the controller can't do.
        raise SystemExit(f"el-devices xcarve: {exc}") from exc


def _status(args: argparse.Namespace) -> int:
    def show(stage: "XCarve") -> int:
        speed = stage.speed()
        print(f"  speed {speed.value:g} mm/s, at most {speed.maximum:g} mm/s")
        print(f"  position {_where(stage.position())}")
        return 0

    return _run(args, show)


def _home(args: argparse.Namespace) -> int:
    def home(stage: "XCarve") -> int:
        print("  homing...")
        stage.home()
        print(f"  homed at {_where(stage.position())}")
        return 0

    return _run(args, home)


def _move(args: argparse.Namespace) -> int:
    targets = {axis: getattr(args, axis) for axis in DEFAULT_LIMITS}
    targets = {axis: value for axis, value in targets.items() if value is not None}
    if not targets:
        raise SystemExit("el-devices xcarve move: give at least one of --x, --y, --z")

    def move(stage: "XCarve") -> int:
        # Fail on a bad target now, not after homing.
        for axis in stage.axes:
            if axis.name in targets:
                axis.check(targets[axis.name])
        print("  homing...")
        stage.home()
        print(f"  homed at {_where(stage.position())}")
        print(f"  moving to {_where(targets)}...")
        stage.move_to(targets)
        print(f"  at {_where(stage.position())}")
        return 0

    return _run(args, move)


def _check(args: argparse.Namespace) -> int:
    from educelab.devices.stages.xcarve import ARRIVAL_TOLERANCE_MM

    def check(stage: "XCarve") -> int:
        print("  homing...")
        stage.home()
        home = stage.position()
        print(f"  homed at {_where(home)}")
        failures = 0
        for axis in stage.axes:
            assert axis.minimum is not None and axis.maximum is not None
            middle = (axis.minimum + axis.maximum) / 2
            step = args.distance if middle > home[axis.name] else -args.distance
            for target in (home[axis.name] + step, home[axis.name]):
                stage.move_to({axis.name: target})
                reached = stage.position()[axis.name]
                ok = abs(reached - target) <= ARRIVAL_TOLERANCE_MM
                failures += not ok
                status = "ok" if ok else f"reached {reached:g}"
                print(f"  {axis.name} to {target:g}: {status}")
        return 1 if failures else 0

    return _run(args, check)


def _where(position: dict[str, float]) -> str:
    return ", ".join(f"{axis}={value:g}" for axis, value in position.items())
