"""Force a stalled stream on one camera, and check the driver reports and clears it.

Task 3.3 of track gentl-stall_20261109. Stops the camera's acquisition behind
the driver's back (AcquisitionStop), expects the next capture to raise
StreamStalled, then expects frames again after restart_acquisition().

Usage: python force-stall.py [--backend gentl|spinnaker] [--serial SERIAL]
Exits 0 if every step went as expected.
"""

import argparse
import logging
import sys
import time

from educelab.devices.errors import StreamStalled


def camera_for(backend: str, serial: str | None):
    if backend == "gentl":
        from educelab.devices.cameras.gentl import GenTLCamera as kind
    else:
        from educelab.devices.cameras.spinnaker import SpinnakerCamera as kind
    if serial is None:
        found = kind.discover()
        if not found:
            sys.exit("no camera found")
        serial = found[0].serial_number
    return kind(serial)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backend", choices=["gentl", "spinnaker"], default="gentl")
    parser.add_argument("--serial")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    ok = True
    with camera_for(args.backend, args.serial) as camera:
        camera.capture_timeout = 2.0
        print(f"{camera.info.serial_number}: link {camera.setting('link_speed').value}")
        camera.capture_frame()
        print(f"before: {camera.diagnostics()}")

        # Behind the driver's back: the camera stops, the host stream doesn't.
        camera._connection.execute("AcquisitionStop")
        start = time.perf_counter()
        try:
            camera.capture_frame()
        except StreamStalled as exc:
            print(f"stalled after {time.perf_counter() - start:.1f} s, as expected:")
            print(f"  {exc}")
            print(f"  counters: {exc.counters}")
        except Exception as exc:  # noqa: BLE001
            print(f"UNEXPECTED {type(exc).__name__}: {exc}")
            ok = False
        else:
            print("UNEXPECTED: a frame came after AcquisitionStop")
            ok = False

        camera.restart_acquisition()
        try:
            frames = [camera.capture_frame() for _ in range(10)]
        except Exception as exc:  # noqa: BLE001
            print(f"UNEXPECTED after the restart, {type(exc).__name__}: {exc}")
            ok = False
        else:
            ids = [f.metadata["frame_id"] for f in frames]
            print(f"after the restart: 10 frames, ids {ids[0]}..{ids[-1]}")
        print(f"after: {camera.diagnostics()}")
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
