"""Run the preview hardware tests on this machine and save what they print.

From the repository root, in an environment with the package installed
(``pip install -e . --group dev`` plus the backend's SDK)::

    python conductor/tracks/preview-mode_20261102/hardware_tests.py

By default it runs the preview tests through GenTL on every camera GenTL finds.
Point the cameras at a lit, still scene with detail in it, lens caps off.

    --backend gentl spinnaker    which backends to run (gentl, spinnaker, aravis)
    --serial 4110091493 ...      only these cameras, as the backend lists them
    --all                        the whole hardware suite, not only preview
    --out DIR                    where the logs go (default: hardware-logs)

Send back the ``hardware-logs`` folder.
"""

import argparse
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BACKENDS = {
    "gentl": ("GENTL_SERIAL", "educelab.devices.cameras.gentl", "GenTLCamera"),
    "spinnaker": (
        "SPINNAKER_SERIAL",
        "educelab.devices.cameras.spinnaker",
        "SpinnakerCamera",
    ),
    "aravis": ("ARAVIS_SERIAL", "educelab.devices.cameras.aravis", "AravisCamera"),
}
PREVIEW = "preview or region or max_size or eight_bit"


def discover(backend: str) -> list[str]:
    """What the backend lists, one ``serial<TAB>description`` per camera. Run
    in its own process, so nothing holds the cameras when the tests start."""
    _, module, name = BACKENDS[backend]
    code = (
        f"from {module} import {name} as C\n"
        "for i in C.discover():\n"
        "    print(f'{i.serial_number}\\t{i.vendor} {i.model}')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT
    )
    if done.returncode:
        lines = done.stderr.strip().splitlines()
        print(f"  {backend}: not available ({lines[-1] if lines else 'no output'})")
        return []
    return [line for line in done.stdout.splitlines() if "\t" in line]


def run(backend: str, serial: str, everything: bool, out: Path) -> bool:
    variable = BACKENDS[backend][0]
    command = [sys.executable, "-m", "pytest", f"tests/test_{backend}_hardware.py"]
    command += ["-m", "hardware", "-s", "-rs", "-v", "-p", "no:cacheprovider"]
    if not everything:
        command += ["-k", PREVIEW]
    log = out / f"{backend}-{serial}.log"
    print(f"\n=== {backend} {serial} -> {log}", flush=True)
    with log.open("w", encoding="utf-8") as file:
        file.write(f"{' '.join(command)}\n{variable}={serial}\n\n")
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env={**os.environ, variable: serial},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            file.write(line)
        return process.wait() == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backend", nargs="+", choices=BACKENDS, default=["gentl"])
    parser.add_argument("--serial", nargs="+", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--out", type=Path, default=Path("hardware-logs"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    ).stdout.strip()
    summary = [
        f"{datetime.now():%Y-%m-%d %H:%M} on {platform.node()}",
        f"{platform.platform()}, Python {platform.python_version()}, commit {head}",
        f"GENICAM_GENTL64_PATH={os.environ.get('GENICAM_GENTL64_PATH', '')}",
    ]
    print("\n".join(summary))
    print("\nCameras found:")
    results: list[tuple[str, str, bool]] = []
    for backend in args.backend:
        cameras = discover(backend)
        for line in cameras:
            print(f"  {backend}: {line}")
        summary += [f"{backend}: {line}" for line in cameras]
        serials = [line.split("\t")[0] for line in cameras]
        missing = [s for s in args.serial if s not in serials]
        if args.serial and missing and len(args.backend) == 1:
            print(f"  {backend}: no camera {', '.join(missing)}")
        for serial in serials:
            if not args.serial or serial in args.serial:
                results.append((backend, serial, False))

    if not results:
        print("\nNo cameras to test.")
        return 2
    for index, (backend, serial, _) in enumerate(results):
        results[index] = (backend, serial, run(backend, serial, args.all, args.out))

    print("\nSummary:")
    for backend, serial, passed in results:
        line = f"{'passed' if passed else 'FAILED'}  {backend} {serial}"
        print(f"  {line}")
        summary.append(line)
    (args.out / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print(f"\nLogs are in {args.out.resolve()}")
    return 0 if all(passed for *_, passed in results) else 1


if __name__ == "__main__":
    sys.exit(main())
