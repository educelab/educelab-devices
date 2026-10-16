"""``el-devices foldio``: check a Foldio360 turntable and its halo by hand."""

import argparse
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

# Imported where it's used, so other subcommands don't load the driver.
if TYPE_CHECKING:
    from educelab.devices.stages.foldio360 import Foldio360

# Wire direction values and firmware speeds, repeated here so parsing the
# command line doesn't import the driver (docs/hardware/foldio360.md, rotate).
_CW, _CCW = "CW", "CCW"
_SPEEDS = (1, 2, 3)


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "foldio",
        help="check a Foldio360 turntable and its halo",
        description="Report, turn, and light a Foldio360 turntable over Bluetooth. "
        "Without --address, scans for one first, which takes the scan window. "
        "Close the official app first: only one host can hold the turntable.",
    )
    parser.add_argument(
        "--address",
        help="BLE address to connect to, skipping the scan; on macOS, the "
        "per-host UUID that 'scan' shows",
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=8.0,
        metavar="SECONDS",
        help="how long to scan (default: 8)",
    )
    actions = parser.add_subparsers(
        title="actions", dest="action", metavar="ACTION", required=True
    )

    actions.add_parser(
        "scan",
        help="list advertising BLE devices, marking turntables",
        description="List every advertising BLE device, strongest first, without "
        "connecting. Use it to find the address to pass to --address.",
    ).set_defaults(func=_scan)

    actions.add_parser(
        "status", help="show firmware, position, and halo level"
    ).set_defaults(func=_status)

    speed = argparse.ArgumentParser(add_help=False)
    speed.add_argument(
        "-s",
        "--speed",
        type=int,
        choices=_SPEEDS,
        default=1,
        help="firmware speed, 1 (slowest) to 3 (default: 1)",
    )

    rotate = actions.add_parser(
        "rotate",
        parents=[speed],
        help="turn by an angle",
        description="Turn by an angle, literally: 720 turns twice.",
    )
    rotate.add_argument("direction", type=parse_direction, help="CW (clockwise) or CCW")
    rotate.add_argument("degrees", type=bounded_int(1, label="degrees"))
    rotate.set_defaults(func=_rotate)

    goto = actions.add_parser(
        "goto",
        parents=[speed],
        help="turn to an angle the shorter way round",
        description="Turn to an angle of the position counter, the shorter way "
        "round. The counter re-zeros wherever the platter sits at power-on.",
    )
    goto.add_argument("angle", type=bounded_int(0, 359, "angle"))
    goto.set_defaults(func=_goto)

    halo = actions.add_parser("halo", help="set the halo's level")
    halo.add_argument("level", type=_level, help="0 (off) to 1 (full)")
    halo.set_defaults(func=_halo)

    actions.add_parser(
        "stop",
        help="stop any motion, including another program's jog; the halo stays as it is",
    ).set_defaults(func=_stop)


def parse_direction(value: str) -> str:
    """Accept CW or CCW in any case, and refuse the official app's labels.

    The app labels CW "Left" and CCW "Right", so accepting those words would
    bake its inversion into scripts.
    """
    upper = value.upper()
    if upper in ("LEFT", "RIGHT"):
        wire = _CW if upper == "LEFT" else _CCW
        raise argparse.ArgumentTypeError(
            f"refusing {value!r}: the official app's Left/Right labels are inverted "
            f"against the wire values (its {upper.title()} sends {wire}). Say "
            f"{_CW} or {_CCW}."
        )
    if upper not in (_CW, _CCW):
        raise argparse.ArgumentTypeError(
            f"direction must be {_CW} or {_CCW}, not {value!r}"
        )
    return upper


def bounded_int(
    low: int, high: int | None = None, label: str = "value"
) -> Callable[[str], int]:
    """An argparse type that range-checks an int before anything connects."""

    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{label} must be an integer, not {value!r}"
            ) from None
        if number < low or (high is not None and number > high):
            span = f"{low} to {high}" if high is not None else f"at least {low}"
            raise argparse.ArgumentTypeError(f"{label} must be {span}, not {number}")
        return number

    return parse


def _level(text: str) -> float:
    try:
        level = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not 0.0 <= level <= 1.0:
        raise argparse.ArgumentTypeError(f"must be from 0 to 1, not {level}")
    return level


def _turntable(args: argparse.Namespace) -> "Foldio360":
    from educelab.devices.stages.foldio360 import Foldio360

    return Foldio360(args.address, scan_timeout=args.scan_timeout)


def _scan(args: argparse.Namespace) -> int:
    from educelab.devices.stages.foldio360 import EMPTY_SCAN_NOTE, scan_lines

    lines = scan_lines(args.scan_timeout)
    if not lines:
        print(EMPTY_SCAN_NOTE.format(seconds=args.scan_timeout))
        return 1
    for line in lines:
        print(line)
    return 0


def _status(args: argparse.Namespace) -> int:
    with _turntable(args) as table, table.halo:
        info = table.info
        print(f"{info.model} at {info.address}, firmware {info.firmware_version}")
        print(f"  position: {_degrees(table)} (re-zeroed at power-on)")
        print(f"  halo: {table.halo.level('halo') or 0.0:.0%}")
    return 0


def _rotate(args: argparse.Namespace) -> int:
    offset = args.degrees if args.direction == _CW else -args.degrees
    with _turntable(args) as table:
        _set_speed(table, args.speed)
        start = _degrees(table)
        _move(table, table.move_by, {"theta": offset})
        print(
            f"turned {args.direction} {args.degrees}° from {start}: now at {_degrees(table)}"
        )
    return 0


def _goto(args: argparse.Namespace) -> int:
    with _turntable(args) as table:
        _set_speed(table, args.speed)
        start = _degrees(table)
        _move(table, table.move_to, {"theta": args.angle})
        print(f"turned from {start} to {_degrees(table)}")
    return 0


def _halo(args: argparse.Namespace) -> int:
    with _turntable(args) as table, table.halo:
        table.halo.set_level("halo", args.level)
        print(f"halo at {table.halo.level('halo') or 0.0:.0%}")
    return 0


def _stop(args: argparse.Namespace) -> int:
    with _turntable(args) as table:
        table.stop()
        print(f"stopped at {_degrees(table)}")
    return 0


def _set_speed(table: "Foldio360", level: int) -> None:
    choices = table.speed().choices or ()
    table.set_speed(float(choices[level - 1]))


def _move(
    table: "Foldio360",
    move: Callable[[Mapping[str, float]], None],
    targets: Mapping[str, float],
) -> None:
    """Make a move, stopping the turntable if the user presses Ctrl-C."""
    try:
        move(targets)
    except KeyboardInterrupt:
        print("interrupted; stopping the turntable")
        table.stop()
        raise


def _degrees(table: "Foldio360") -> str:
    return f"{table.position()['theta']:.0f}°"
