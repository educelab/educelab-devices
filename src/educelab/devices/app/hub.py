"""``el-devices hub``: list Acroname hubs, show and switch port power and speed."""

import argparse
import functools
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

# Imported where it's used, so other subcommands don't load the driver.
if TYPE_CHECKING:
    from educelab.devices.hubs.acroname import AcronameHub, PortSpeed


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "hub",
        help="list Acroname USB hubs, show port power and speed, power-cycle a "
        "port, or limit its speed",
        description="Check an Acroname USBHub3p. Without an action, shows the "
        "status of the one hub attached.",
    )
    parser.set_defaults(func=_status, serial_number=None)
    actions = parser.add_subparsers(title="actions", metavar="ACTION")

    actions.add_parser("list", help="list attached hubs").set_defaults(func=_list)

    status = actions.add_parser(
        "status", help="show a hub's identity and every port's power and speed"
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

    speed = actions.add_parser(
        "speed",
        help="show or limit one port's USB speed",
        description="Show a port's speed limit and the speed its device connected "
        "at, or set the limit. Lowering the limit applies at once. Raising it to "
        "usb3 doesn't bring back a device that dropped to USB 2: add --cycle to "
        "power-cycle the port afterwards.",
    )
    _add_serial_number(speed)
    speed.add_argument("port", type=int, help="port number, from 0")
    speed.add_argument(
        "speed",
        nargs="?",
        type=_speed,
        help="usb3 (default), usb2, or data-off (power only); omit to show",
    )
    speed.add_argument(
        "--cycle",
        action="store_true",
        help="power-cycle the port after setting, so its device reconnects",
    )
    speed.set_defaults(func=_port_speed)


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


def _speed(text: str) -> "PortSpeed":
    from educelab.devices.hubs.acroname import PortSpeed

    try:
        return PortSpeed(text.lower())
    except ValueError:
        choices = ", ".join(s.value for s in PortSpeed)
        raise argparse.ArgumentTypeError(
            f"not a port speed: {text!r} (choose from {choices})"
        ) from None


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


@_needs_sdk
def _port_speed(args: argparse.Namespace) -> int:
    from educelab.devices.hubs.acroname import AcronameHub

    if args.cycle and args.speed is None:
        print("el-devices: --cycle needs a speed to set", file=sys.stderr)
        return 2
    with AcronameHub(args.serial_number) as hub:
        try:
            if args.speed is not None:
                hub.set_port_speed(args.port, args.speed)
                print(f"Limited port {args.port} to {args.speed}.")
                if args.cycle:
                    hub.power_cycle(args.port)
                    print(f"Power-cycled port {args.port}.")
                elif args.speed == "usb3" and (
                    hub.connected_speed(args.port) == "usb2"
                ):
                    print(
                        f"el-devices: the device on port {args.port} is still at "
                        "USB 2. If it's a USB 3 device, power-cycle the port to "
                        f"reconnect it: el-devices hub speed {args.port} usb3 --cycle",
                        file=sys.stderr,
                    )
            print(f"  {_port_line(hub, args.port)}")
        except KeyError as exc:
            print(f"el-devices: {exc.args[0]}", file=sys.stderr)
            return 1
    return 0


def _print_status(hub: "AcronameHub") -> None:
    info = hub.info
    delay = hub.enumeration_delay().value
    print(
        f"{info.model}, serial {info.serial_number}, firmware "
        f"{info.firmware_version}, enumeration delay {delay:g} s"
    )
    for port in hub.ports:
        print(f"  {_port_line(hub, port)}")


def _port_line(hub: "AcronameHub", port: int) -> str:
    power = "on" if hub.is_powered(port) else "off"
    connected = hub.connected_speed(port) or "nothing"
    return f"port {port}: {power}, limit {hub.port_speed(port)}, connected {connected}"
