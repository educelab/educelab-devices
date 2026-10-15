"""``el-devices hub``: list Acroname hubs, show port power, and power-cycle a port."""

import argparse
import functools
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

# Imported where it's used, so other subcommands don't load the driver.
if TYPE_CHECKING:
    from educelab.devices.hubs.acroname import AcronameHub


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "hub",
        help="list Acroname USB hubs, show port power, or power-cycle a port",
        description="Check an Acroname USBHub3p. Without an action, shows the "
        "status of the one hub attached.",
    )
    parser.set_defaults(func=_status, serial_number=None)
    actions = parser.add_subparsers(title="actions", metavar="ACTION")

    actions.add_parser("list", help="list attached hubs").set_defaults(func=_list)

    status = actions.add_parser(
        "status", help="show a hub's identity and the power state of every port"
    )
    _add_serial_number(status)
    status.set_defaults(func=_status)

    cycle = actions.add_parser("cycle", help="power-cycle one port")
    _add_serial_number(cycle)
    cycle.add_argument("port", type=int, help="port number, from 0")
    cycle.add_argument(
        "--off-time",
        type=float,
        default=None,
        help="seconds to leave the port off (default: the driver's, 1 s)",
    )
    cycle.set_defaults(func=_cycle)


def _add_serial_number(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--serial-number",
        type=_serial_number,
        help="the hub's serial number in hex, e.g. 0x1234ABCD; needed when "
        "several hubs are attached",
    )


def _serial_number(text: str) -> int:
    from educelab.devices.hubs.acroname import parse_serial_number

    try:
        return parse_serial_number(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _needs_sdk(
    action: Callable[[argparse.Namespace], int],
) -> Callable[[argparse.Namespace], int]:
    """Report a missing or unloadable BrainStem SDK without a traceback."""

    @functools.wraps(action)
    def wrapper(args: argparse.Namespace) -> int:
        try:
            return action(args)
        except ImportError as exc:
            print(f"el-devices: {exc}", file=sys.stderr)
            return 1

    return wrapper


@_needs_sdk
def _list(args: argparse.Namespace) -> int:
    from educelab.devices.hubs.acroname import AcronameHub

    found = AcronameHub.discover()
    for info in found:
        print(f"{info.model}, serial {info.serial_number}")
    if not found:
        print("No USBHub3p hubs found.")
    return 0


@_needs_sdk
def _status(args: argparse.Namespace) -> int:
    from educelab.devices.hubs.acroname import AcronameHub

    with AcronameHub(args.serial_number) as hub:
        _print_status(hub)
    return 0


@_needs_sdk
def _cycle(args: argparse.Namespace) -> int:
    from educelab.devices.hubs.acroname import AcronameHub

    with AcronameHub(args.serial_number) as hub:
        try:
            hub.power_cycle(args.port, off_time=args.off_time)
        except (KeyError, ValueError) as exc:
            print(f"el-devices: {exc.args[0]}", file=sys.stderr)
            return 1
        print(f"Power-cycled port {args.port}.")
        _print_status(hub)
    return 0


def _print_status(hub: "AcronameHub") -> None:
    info = hub.info
    delay = hub.enumeration_delay().value
    print(
        f"{info.model}, serial {info.serial_number}, firmware "
        f"{info.firmware_version}, enumeration delay {delay:g} s"
    )
    for port in hub.ports:
        print(f"  port {port}: {'on' if hub.is_powered(port) else 'off'}")
