"""Time gphoto2 setting reads and writes on a real camera (Task 2.1).

    python conductor/tracks/gphoto2-single-setting_20261102/time_settings.py
    python conductor/tracks/gphoto2-single-setting_20261102/time_settings.py \
        --port usb:020,007 --repeats 10

Set the camera to M with a numeric shutter speed and ISO (not Bulb or Auto).
Each write is undone, so the camera ends where it started. Paste the output
into the track or send it over for the hardware doc.
"""

import argparse
import platform
import statistics
import time
from collections.abc import Callable

from educelab.devices.cameras import EXPOSURE_TIME, ISO
from educelab.devices.cameras.gphoto2 import GPhoto2Camera


def timed(label: str, repeats: int, call: Callable[[], object]) -> None:
    times = []
    for _ in range(repeats):
        started = time.monotonic()
        call()
        times.append(time.monotonic() - started)
    print(
        f"{label:<34} median {statistics.median(times):6.3f} s  "
        f"min {min(times):6.3f}  max {max(times):6.3f}  (n={repeats})"
    )


def neighbour(camera: GPhoto2Camera, name: str):
    """The current value of a canonical setting, and a choice next to it."""
    setting = camera.setting(name)
    choices = list(setting.choices or ())
    if setting.value is None or setting.value not in choices or len(choices) < 2:
        raise SystemExit(
            f"{name} is {setting.value!r}; set the camera to M with a numeric "
            "shutter speed and ISO, then run again."
        )
    i = choices.index(setting.value)
    return setting.value, choices[i - 1 if i else 1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", help="libgphoto2 port; default: the only camera")
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    n = args.repeats

    camera = GPhoto2Camera(args.port)
    started = time.monotonic()
    camera.open()
    print(f"opened in {time.monotonic() - started:.3f} s")
    try:
        info = camera.info
        print(f"{info.model}, firmware {info.firmware_version}, port {info.address}")
        print(f"host: {platform.platform()}, python {platform.python_version()}\n")

        # The first read has no mapping yet, so it reads the whole tree.
        started = time.monotonic()
        camera.setting(EXPOSURE_TIME)
        print(
            f"{'setting() before any full read':<34} {time.monotonic() - started:.3f} s"
        )
        timed("settings() (whole tree)", n, camera.settings)
        print(f"  {len(camera.settings())} settings\n")

        timed("setting(exposure_time)", n, lambda: camera.setting(EXPOSURE_TIME))
        timed("setting(iso)", n, lambda: camera.setting(ISO))
        timed("setting(imagequality)", n, lambda: camera.setting("imagequality"))

        for name in (EXPOSURE_TIME, ISO):
            original, other = neighbour(camera, name)
            values = iter([other, original] * n)
            try:
                timed(
                    f"set_setting({name})",
                    2 * n,
                    lambda name=name, values=values: camera.set_setting(
                        name, next(values)
                    ),
                )
            finally:
                camera.set_setting(name, original)
            print(f"  {name}: {original!r} <-> {other!r}")
    finally:
        camera.close()


if __name__ == "__main__":
    main()
