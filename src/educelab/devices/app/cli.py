"""Entry point for ``el-devices``.

Each subcommand is a module in this package with a ``register(subparsers)``
function that adds its parser and sets ``func``. Subcommand modules import
their backends inside ``func``, so the CLI starts without any vendor SDK.
"""

import argparse
import importlib
import logging
import sys
from collections.abc import Sequence

from educelab.devices import __version__
from educelab.devices.errors import DeviceError

SUBCOMMANDS = (
    "discover",
    "dslr",
    "foldio",
    "genicam",
    "hub",
    "misha",
    "simulate",
    "spinnaker",
    "xcarve",
    "xlc",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="el-devices",
        description="Check EduceLab imaging hardware from the command line.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="log device traffic"
    )
    subparsers = parser.add_subparsers(
        title="commands", dest="command", metavar="COMMAND", required=True
    )
    for name in SUBCOMMANDS:
        importlib.import_module(f"educelab.devices.app.{name}").register(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args) or 0)
    except DeviceError as exc:
        print(f"el-devices: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
