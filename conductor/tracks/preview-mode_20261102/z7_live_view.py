"""Live-view discovery on a gphoto2 body such as the Z7 II (Tasks 1.1-1.3).

    python conductor/tracks/preview-mode_20261102/z7_live_view.py viewfinder
    python conductor/tracks/preview-mode_20261102/z7_live_view.py zoom --out z7-zoom
    python conductor/tracks/preview-mode_20261102/z7_live_view.py settle --steps 200

`viewfinder` asks you what the rear screen and EVF show at each step, so run it
at the camera. `zoom` saves a JPEG for every size, zoom level and zoom center it
tries; point the camera at a flat, detailed target (a ruler or grid). `settle`
needs a lens that drives focus, with the camera in AF-S. Paste the output (and
send the `zoom` directory) back for the hardware doc.
"""

import argparse
import hashlib
import io
import platform
import time
from pathlib import Path

import numpy as np
from PIL import Image

from educelab.devices.cameras.gphoto2 import GPhoto2Camera

SIZES = ("QVGA", "VGA", "XGA")
ZOOMS = ("0", "512", "1024", "2048")
KEYWORDS = ("liveview", "zoom", "afarea", "viewfinder", "focusarea", "lvzoom")


class Locked:
    """The driver's session, with each call under the camera's lock so the
    keep-alive thread can't talk to the camera at the same time."""

    def __init__(self, camera: GPhoto2Camera) -> None:
        self._camera = camera

    def __getattr__(self, name: str):
        method = getattr(self._camera._require_session(), name)

        def call(*args, **kwargs):
            with self._camera._lock:
                return method(*args, **kwargs)

        return call


def ask(prompt: str) -> None:
    answer = input(f"  >> {prompt}\n     what do you see? ")
    print(f"     answer: {answer}")


def viewfinder_value(session) -> object:
    widget = session.widget("viewfinder")
    return None if widget is None else widget.value


def preview(session) -> tuple[bytes, float]:
    started = time.monotonic()
    data = session.capture_preview()
    return data, time.monotonic() - started


def decode(data: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(data)) as image:
        return np.asarray(image.convert("L"), dtype=np.float64)


def sharpness(gray: np.ndarray) -> float:
    """Variance of the Laplacian, the score used in the gphoto2 focus sweep."""
    lap = (
        -4 * gray[1:-1, 1:-1]
        + gray[:-2, 1:-1]
        + gray[2:, 1:-1]
        + gray[1:-1, :-2]
        + gray[1:-1, 2:]
    )
    return float(lap.var())


def digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:10]


def dump_widgets(session) -> None:
    print("live-view widgets:")
    for name, w in sorted(session.config().items()):
        if any(k in name.lower() for k in KEYWORDS):
            extra = ""
            if w.choices:
                extra = f" choices={list(w.choices)}"
            elif w.minimum is not None:
                extra = f" range=[{w.minimum}, {w.maximum}] step={w.step}"
            ro = " read-only" if w.read_only else ""
            print(f"  {name:<22} {w.kind!s:<12} {w.value!r} [{w.section}]{ro}{extra}")


def run_viewfinder(camera: GPhoto2Camera, session) -> None:
    print(f"viewfinder at open: {viewfinder_value(session)!r}")
    ask("Before any preview. Rear screen and EVF")

    for i in range(3):
        data, took = preview(session)
        print(f"capture_preview {i + 1}: {len(data)} B in {took:.3f} s")
    print(f"viewfinder after previews alone: {viewfinder_value(session)!r}")
    ask("After 3 capture_preview() calls, viewfinder untouched")

    print("waiting 10 s with no previews")
    time.sleep(10)
    ask("10 s after the last preview. Is live view still up")

    session.set_config({"viewfinder": 1})
    print(f"viewfinder=1 written; reads {viewfinder_value(session)!r}")
    ask("After viewfinder=1")
    data, took = preview(session)
    print(f"capture_preview with viewfinder=1: {len(data)} B in {took:.3f} s")

    session.set_config({"viewfinder": 0})
    print(f"viewfinder=0 written; reads {viewfinder_value(session)!r}")
    ask("After viewfinder=0")

    data, took = preview(session)
    print(f"capture_preview after viewfinder=0: {len(data)} B in {took:.3f} s")
    print(f"viewfinder now reads {viewfinder_value(session)!r}")
    ask("After a preview following viewfinder=0. Did live view come back")

    session.set_config({"viewfinder": 0})
    print("viewfinder=0 written again before close")
    ask("Final state before close")


def save(out: Path, name: str, data: bytes) -> tuple[int, int, float]:
    path = out / f"{name}.jpg"
    path.write_bytes(data)
    gray = decode(data)
    return gray.shape[1], gray.shape[0], sharpness(gray)


def settle_frames(session, n: int = 3) -> bytes:
    """Discard a few frames after a change so the saved one reflects it."""
    data = b""
    for _ in range(n):
        data, _ = preview(session)
    return data


def run_zoom(camera: GPhoto2Camera, session, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    dump_widgets(session)
    original = {n: session.value(n) for n in ("liveviewsize", "liveviewzoomarea")}
    center = session.widget("changeafarea")
    print(f"\noriginal: {original}; changeafarea: {center!r}")
    try:
        print("\nsize x zoom (frame size, JPEG bytes, sharpness):")
        for size in SIZES:
            session.set_config({"liveviewsize": size})
            for zoom in ZOOMS:
                session.set_config({"liveviewzoomarea": zoom})
                data = settle_frames(session)
                w, h, s = save(out, f"{size}-zoom{zoom}", data)
                print(f"  {size:<5} zoom {zoom:<5} {w}x{h} {len(data):>7} B  {s:9.1f}")

        if center is None:
            print("\nno changeafarea widget; skipping zoom-center checks")
            return
        # libgphoto2's Nikon driver takes "XxY" in sensor pixels for this
        # widget. Try the corners and centre at the tightest zoom.
        session.set_config({"liveviewsize": "XGA", "liveviewzoomarea": "512"})
        print("\nchangeafarea at XGA, zoom 512:")
        for value in ("0x0", "4128x2752", "8256x5504", "1000x1000", "7000x4500"):
            try:
                session.set_config({"changeafarea": value})
            except Exception as exc:  # noqa: BLE001 - recording what fails
                print(f"  {value:<10} write failed: {exc}")
                continue
            data = settle_frames(session)
            w, h, s = save(out, f"XGA-zoom512-af{value}", data)
            reads = session.value("changeafarea")
            print(f"  {value:<10} reads {reads!r}; {w}x{h} sharpness {s:.1f}")
        ask("Did the zoomed view move between the changeafarea writes")
    finally:
        session.set_config({k: v for k, v in original.items() if v is not None})
        if center is not None and center.value:
            try:
                session.set_config({"changeafarea": center.value})
            except Exception as exc:  # noqa: BLE001
                print(f"could not restore changeafarea: {exc}")
        print(f"restored {original}")


def run_settle(camera: GPhoto2Camera, session, steps: int, rounds: int) -> None:
    focus = camera.focus
    if focus is None:
        raise SystemExit("this camera has no focus drive")
    session.set_config({"liveviewsize": "XGA"})
    print("baseline: 10 previews, no focus change")
    last = None
    for i in range(10):
        data, took = preview(session)
        h = digest(data)
        same = "same" if h == last else "new"
        print(f"  {i:>2} {took:.3f} s {h} {same:<4} {sharpness(decode(data)):9.1f}")
        last = h

    for r in range(rounds):
        move = steps if r % 2 == 0 else -steps
        settle_frames(session, 2)
        before = sharpness(decode(preview(session)[0]))
        started = time.monotonic()
        focus.step(move)
        stepped = time.monotonic()
        moved = stepped - started
        print(f"\nround {r + 1}: step({move}) took {moved:.3f} s; before {before:.1f}")
        last = None
        for i in range(15):
            data, took = preview(session)
            h = digest(data)
            same = "same" if h == last else "new"
            s = sharpness(decode(data))
            since = time.monotonic() - stepped
            print(f"  {i:>2} +{since:6.3f} s {took:.3f} s {h} {same:<4} {s:9.1f}")
            last = h


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("check", choices=("viewfinder", "zoom", "settle"))
    parser.add_argument("--port", help="libgphoto2 port; default: the only camera")
    parser.add_argument("--out", type=Path, default=Path("z7-zoom"))
    parser.add_argument("--steps", type=int, default=200, help="settle: focus step")
    parser.add_argument("--rounds", type=int, default=4, help="settle: moves")
    args = parser.parse_args()

    camera = GPhoto2Camera(args.port)
    camera.open()
    try:
        info = camera.info
        print(f"{info.model}, firmware {info.firmware_version}, port {info.address}")
        print(f"host: {platform.platform()}, python {platform.python_version()}\n")
        session = Locked(camera)
        if args.check == "viewfinder":
            run_viewfinder(camera, session)
        elif args.check == "zoom":
            run_zoom(camera, session, args.out)
        else:
            run_settle(camera, session, args.steps, args.rounds)
    finally:
        camera.close()


if __name__ == "__main__":
    main()
