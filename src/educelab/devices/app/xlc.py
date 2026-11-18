"""``el-devices xlc``: check an XLC2 light controller by lighting each channel."""

import argparse
import time


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "xlc",
        help="light each channel of an XLC2 in turn",
        description="Identify an XLC2, light its white strip and then its 940 nm "
        "array, and turn both off. The 940 nm array is invisible to the eye; "
        "check it with a camera. With no --port or --serial-number, the XLC2 "
        "is found by probing, which resets every board with an XLC2's USB "
        "bridge.",
    )
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--port", help="serial port, e.g. /dev/cu.usbmodem00001")
    where.add_argument(
        "--serial-number", help="the board's unit ID, as set with --set-id"
    )
    parser.add_argument(
        "--set-id",
        metavar="ID",
        type=_unit_id,
        help="store this unit ID on the board instead of lighting it "
        "(1-16 letters, digits, '_' or '-'; firmware 2.1.0 or newer)",
    )
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


def _unit_id(text: str) -> str:
    from educelab.devices.lights.xlc import UNIT_ID

    if not UNIT_ID.fullmatch(text):
        raise argparse.ArgumentTypeError(
            f"must be 1-16 letters, digits, '_' or '-', not {text!r}"
        )
    return text


def run(args: argparse.Namespace) -> int:
    # Imported here, so other subcommands don't load the driver.
    from educelab.devices.errors import DeviceError, DeviceNotFound
    from educelab.devices.lights.xlc import XLC2

    port = args.port
    if port is None and args.serial_number is None:
        found = XLC2.probe()
        if not found:
            raise DeviceNotFound(
                "no XLC2 answered. Ports in use by another program are skipped, "
                "so close it, or give the port with --port"
            )
        if len(found) > 1:
            boards = ", ".join(
                f"{i.serial_number or '(no ID)'} at {i.address}" for i in found
            )
            raise DeviceError(
                f"found {len(found)} XLC2s ({boards}); choose one with --port "
                "or --serial-number"
            )
        port = found[0].address

    with XLC2(port, serial_number=args.serial_number) as light:
        info = light.info
        print(
            f"{info.model} {info.serial_number or '(no ID)'} at {info.address}, "
            f"firmware {info.firmware_version}, hardware {light.hardware_version}"
        )
        if args.set_id is not None:
            light.set_unit_id(args.set_id)
            print(f"  unit ID set to {args.set_id}")
            return 0
        for channel in light.channels:
            light.set_level(channel.id, args.level)
            print(f"  {channel.id} at {args.level:.0%}")
            time.sleep(args.dwell)
            light.set_level(channel.id, 0.0)
        light.all_off()
        print("  all off")
    return 0
