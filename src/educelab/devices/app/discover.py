"""``el-devices discover``: list devices the installed backends can find."""

import argparse
import importlib
from dataclasses import dataclass

from educelab.devices.device import Device
from educelab.devices.errors import DeviceError


@dataclass(frozen=True)
class Backend:
    """A driver whose `discover()` this command calls.

    Attributes:
        name: Label shown in the output.
        module: Module that defines the driver. It must import without the
            vendor SDK.
        device_class: Name of the driver class in ``module``.
    """

    name: str
    module: str
    device_class: str


# Each driver track adds its backend here.
BACKENDS: tuple[Backend, ...] = (
    Backend("acroname", "educelab.devices.hubs.acroname", "AcronameHub"),
    Backend("foldio360", "educelab.devices.stages.foldio360", "Foldio360"),
    Backend("gphoto2", "educelab.devices.cameras.gphoto2", "GPhoto2Camera"),
    Backend("misha", "educelab.devices.lights.misha", "MishaLight"),
    Backend("spinnaker", "educelab.devices.cameras.spinnaker", "SpinnakerCamera"),
)


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "discover",
        help="list devices the installed backends can find",
        description="List devices that the installed backends can find, "
        "without opening them.",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    found = 0
    for backend in BACKENDS:
        try:
            module = importlib.import_module(backend.module)
            device_class: type[Device] = getattr(module, backend.device_class)
            infos = device_class.discover()
        except ImportError as exc:
            print(f"{backend.name}: unavailable. {exc}")
            continue
        except DeviceError as exc:
            print(f"{backend.name}: discovery failed. {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            # One broken backend shouldn't hide the others.
            print(f"{backend.name}: discovery failed. {type(exc).__name__}: {exc}")
            continue
        for info in infos:
            found += 1
            details = [f"{info.model} at {info.address or 'unknown address'}"]
            if info.serial_number:
                details.append(f"serial {info.serial_number}")
            if info.firmware_version:
                details.append(f"firmware {info.firmware_version}")
            print(f"{backend.name}: {', '.join(details)}")
    if not found:
        print("No devices found.")
    return 0
