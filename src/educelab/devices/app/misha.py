"""``el-devices misha``: check a MISHA panel by lighting each band in turn."""

import argparse
import time
from typing import TYPE_CHECKING

# Imported where it's used, so other subcommands don't load the driver.
if TYPE_CHECKING:
    from educelab.devices.lights.misha import MishaLight


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "misha",
        help="light each band of a MISHA panel in turn",
        description="Identify a MISHA panel, light each band in turn and check that "
        "the panel reports it, then turn everything off. Without --port or "
        "--serial-number, uses the one named panel attached.",
    )
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--port", help="serial port, e.g. COM3 or /dev/ttyUSB0")
    where.add_argument("--serial-number", help="USB serial number, e.g. MISHA-2108d7a8")
    parser.add_argument(
        "--level",
        type=_level,
        default=0.5,
        help="brightness for each band, above 0 and up to 1 (default: 0.5)",
    )
    parser.add_argument(
        "--dwell",
        type=float,
        default=1.0,
        help="seconds to leave each band lit (default: 1)",
    )
    parser.set_defaults(func=run)


def _level(text: str) -> float:
    try:
        level = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not 0.0 < level <= 1.0:
        raise argparse.ArgumentTypeError(f"must be above 0 and up to 1, not {level}")
    return level


def run(args: argparse.Namespace) -> int:
    from educelab.devices.lights.misha import MishaLight

    light = MishaLight(args.port, serial_number=args.serial_number)
    mismatches = 0
    with light:
        info = light.info
        print(
            f"{info.model} at {info.address}, serial {info.serial_number or 'unknown'}, "
            f"firmware {info.firmware_version}, hardware {light.hardware_version}"
        )
        print(f"  lit when opened: {_lit(light)}")
        expected = round(args.level * 100) / 100
        for channel in light.channels:
            light.set_level(channel.id, args.level)
            reported = light.level(channel.id)
            status = "ok" if reported == expected else f"read back {reported}"
            mismatches += status != "ok"
            print(f"  {channel.id} at {expected:.0%}: {status}")
            time.sleep(args.dwell)
        light.all_off()
        after = _lit(light)
        mismatches += after != "nothing"
        print(f"  after all off: {after}")
    return 1 if mismatches else 0


def _lit(light: "MishaLight") -> str:
    for channel in light.channels:
        level = light.level(channel.id)
        if level:
            return f"{channel.id} at {level:.0%}"
    return "nothing"
