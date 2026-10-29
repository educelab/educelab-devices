"""``el-devices discover``: list devices the installed backends can find."""

import argparse
import importlib
from dataclasses import dataclass

from educelab.devices.device import Device, DeviceInfo
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
    Backend("aravis", "educelab.devices.cameras.aravis", "AravisCamera"),
    Backend("foldio360", "educelab.devices.stages.foldio360", "Foldio360"),
    Backend("gentl", "educelab.devices.cameras.gentl", "GenTLCamera"),
    Backend("gphoto2", "educelab.devices.cameras.gphoto2", "GPhoto2Camera"),
    Backend("misha", "educelab.devices.lights.misha", "MishaLight"),
    Backend("spinnaker", "educelab.devices.cameras.spinnaker", "SpinnakerCamera"),
    Backend("xcarve", "educelab.devices.stages.xcarve", "XCarve"),
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
    sections = []
    found = 0
    for backend in BACKENDS:
        try:
            module = importlib.import_module(backend.module)
            device_class: type[Device] = getattr(module, backend.device_class)
            infos = device_class.discover()
        except ImportError as exc:
            sections.append(f"{backend.name}: unavailable\n  {exc}")
            continue
        except DeviceError as exc:
            sections.append(f"{backend.name}: discovery failed\n  {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            # One broken backend shouldn't hide the others.
            sections.append(
                f"{backend.name}: discovery failed\n  {type(exc).__name__}: {exc}"
            )
            continue
        found += len(infos)
        if not infos:
            sections.append(f"{backend.name}: none found")
            continue
        rows = _table(infos)
        sections.append("\n".join([f"{backend.name}: {len(infos)} found", *rows]))
    print("\n\n".join(sections))
    if not found:
        print("No devices found.")
    return 0


_COLUMNS = (
    ("VENDOR", "vendor"),
    ("MODEL", "model"),
    ("SERIAL", "serial_number"),
    ("ADDRESS", "address"),
    ("FIRMWARE", "firmware_version"),
)


def _table(infos: list[DeviceInfo]) -> list[str]:
    """Indented, aligned rows for ``infos``, leaving out columns none of them has."""
    columns = [
        (title, [getattr(info, field) or "" for info in infos])
        for title, field in _COLUMNS
        if any(getattr(info, field) for info in infos)
    ]
    widths = [max(len(title), *map(len, values)) for title, values in columns]
    lines = [[title for title, _ in columns]]
    lines += [[values[i] for _, values in columns] for i in range(len(infos))]
    return [
        "  "
        + "  ".join(
            cell.ljust(w) for cell, w in zip(line, widths, strict=True)
        ).rstrip()
        for line in lines
    ]
