"""``el-devices xlc``: check an XLC2 light controller by lighting each channel."""

import argparse
import time


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "xlc",
        help="light each channel of an XLC2 in turn",
        description="Identify an XLC2, light its white strip and then its 940 nm "
        "array, and turn both off. The 940 nm array is invisible to the eye; "
        "check it with a camera. An XLC2 can't be found automatically, so give "
        "its port.",
    )
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--port", help="serial port, e.g. /dev/cu.usbmodem00001")
    where.add_argument("--serial-number", help="USB serial number of the port")
    parser.add_argument(
        "--level",
        type=_level,
        default=0.25,
        help="level for each channel, above 0 and up to 1 (default: 0.25)",
    )
    parser.add_argument(
        "--dwell",
        type=float,
        default=2.0,
        help="seconds to leave each channel lit (default: 2)",
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
    # Imported here, so other subcommands don't load the driver.
    from educelab.devices.lights.xlc import XLC2

    with XLC2(args.port, serial_number=args.serial_number) as light:
        info = light.info
        print(
            f"{info.model} at {info.address}, firmware {info.firmware_version}, "
            f"hardware {light.hardware_version}"
        )
        for channel in light.channels:
            light.set_level(channel.id, args.level)
            print(f"  {channel.id} at {args.level:.0%}")
            time.sleep(args.dwell)
            light.set_level(channel.id, 0.0)
        light.all_off()
        print("  all off")
    return 0
