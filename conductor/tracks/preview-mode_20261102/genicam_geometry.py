"""ROI, binning and decimation discovery on a GenICam camera (Tasks 1.4, 1.5).

    python conductor/tracks/preview-mode_20261102/genicam_geometry.py spinnaker
    python conductor/tracks/preview-mode_20261102/genicam_geometry.py aravis \
        --serial 4108912345 --exposure 0.02
    python conductor/tracks/preview-mode_20261102/genicam_geometry.py gentl \
        --producer /opt/ids-peak/lib/ids/cti/ids_u3vgentl.cti

Point the camera at an evenly lit, textured scene with nothing saturated and a
mean level around 30-60 (use --exposure), so mean levels show whether binning
averages or sums and offsets show up as image shifts. Each write is tried
while streaming, then checked for frames at the new size; if the camera
refuses it or the stream stops, it's redone with acquisition paused and the
restart is timed. The ROI, binning and decimation are put back as found at the
end, even on failure. It never touches TLParamsLocked.
"""

import argparse
import platform
import time
from collections import Counter

import numpy as np

from educelab.devices.cameras.base import EXPOSURE_TIME
from educelab.devices.errors import DeviceError, DeviceTimeout

GEOMETRY = ("Width", "Height", "OffsetX", "OffsetY")
SIZE_LIMITS = ("WidthMax", "HeightMax", "SensorWidth", "SensorHeight")
SELECTORS = ("BinningSelector", "DecimationSelector")
REDUCTION = (
    "BinningHorizontal",
    "BinningVertical",
    "BinningHorizontalMode",
    "BinningVerticalMode",
    "DecimationHorizontal",
    "DecimationVertical",
    "DecimationHorizontalMode",
    "DecimationVerticalMode",
)
RATE = (
    "AcquisitionFrameRateEnable",
    "AcquisitionFrameRateAuto",
    "AcquisitionFrameRate",
    "AcquisitionResultingFrameRate",
    "DeviceLinkThroughputLimit",
    "DeviceLinkThroughputLimitMode",
    "PixelFormat",
)
LIMIT = "DeviceLinkThroughputLimit"
STATUS: Counter = Counter()


def open_camera(args):
    if args.backend == "spinnaker":
        from educelab.devices.cameras.spinnaker import SpinnakerCamera as cls

        kwargs = {}
    elif args.backend == "aravis":
        from educelab.devices.cameras.aravis import AravisCamera as cls

        kwargs = {}
    else:
        from educelab.devices.cameras.gentl import GenTLCamera as cls

        kwargs = {"producer": args.producer} if args.producer else {}
    serial = args.serial
    if serial is None:
        found = cls.discover(**kwargs) if kwargs else cls.discover()
        if len(found) != 1:
            names = [f"{i.model} {i.serial_number}" for i in found]
            raise SystemExit(f"pass --serial; found {names or 'none'}")
        serial = found[0].serial_number
    camera = cls(serial, **kwargs)
    camera.open()
    return camera


def node_type(conn, name: str) -> str | None:
    """Tell node types apart by their ranges, since some backends read any
    node as a string or an enum."""
    try:
        if conn.enum_entries(name):
            return "enum"
    except (DeviceError, TypeError, ValueError):
        pass
    probes = (
        ("int", conn.int_range),
        ("float", conn.float_range),
        ("int", conn.get_int),  # read-only nodes may have no range
        ("float", conn.get_float),
    )
    for kind, probe in probes:
        try:
            probe(name)
            return kind
        except (DeviceError, TypeError, ValueError):
            continue
    return "string"


def read(conn, name: str):
    kind = node_type(conn, name)
    getter = {
        "enum": conn.get_enum,
        "int": conn.get_int,
        "float": conn.get_float,
        "string": conn.get_string,
    }[kind]
    return getter(name)


def describe(conn, name: str) -> str:
    if not conn.has(name):
        return "absent"
    access = ("R" if conn.is_readable(name) else "-") + (
        "W" if conn.is_writable(name) else "-"
    )
    if not conn.is_readable(name):
        return access
    kind = node_type(conn, name)
    try:
        value = read(conn, name)
    except DeviceError as exc:
        return f"{access} {kind}, unreadable ({exc})"
    try:
        extra = {
            "enum": lambda: f" entries={list(conn.enum_entries(name))}",
            "int": lambda: f" min/max/inc={conn.int_range(name)}",
            "float": lambda: f" min/max={conn.float_range(name)}",
            "string": lambda: "",
        }[kind]()
    except DeviceError:
        extra = ""
    return f"{access} {kind} {value!r}{extra}"


def dump(conn, names, indent: str = "  ") -> None:
    for name in names:
        print(f"{indent}{name:<32} {describe(conn, name)}")


def grab(conn, timeout: float = 2.0):
    """The next complete frame's pixels and id, or None on a timeout. Counts
    every buffer's status in STATUS."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            buffer = conn.grab(max(0.05, deadline - time.monotonic()))
        except DeviceTimeout:
            return None
        try:
            status = buffer.status().name
            STATUS[status] += 1
            if status == "COMPLETE":
                return buffer.pixels(), buffer.frame_id(), buffer.pixel_format()
        finally:
            buffer.release()
    return None


def expected(conn) -> tuple[int, int]:
    return conn.get_int("Height"), conn.get_int("Width")


def frame_at_size(conn, timeout: float = 3.0):
    """The first complete frame matching the Width/Height nodes, and how long
    it took, or (None, timeout)."""
    want = expected(conn)
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        got = grab(conn, deadline - time.monotonic())
        if got is not None and got[0].shape[:2] == want:
            return got, time.monotonic() - started
    return None, timeout


def rate_note(conn) -> str:
    parts = []
    if conn.has(LIMIT) and conn.is_readable(LIMIT):
        _, high, _ = conn.int_range(LIMIT)
        parts.append(
            f"{LIMIT} {conn.get_int(LIMIT) / 1e6:.3f} MB/s (max {high / 1e6:.3f})"
        )
    for name in ("AcquisitionFrameRate", "AcquisitionResultingFrameRate"):
        if conn.has(name) and conn.is_readable(name):
            value = conn.get_float(name)
            if conn.is_writable(name):
                _, high = conn.float_range(name)
                parts.append(f"{name} {value:.2f} (max {high:.2f})")
            else:
                parts.append(f"{name} {value:.2f}")
    return "; ".join(parts) or "no rate nodes"


def measure(conn, seconds: float = 2.0):
    """Frame size, level and rate over a couple of seconds, and the mean."""
    before = STATUS.copy()
    first, _ = frame_at_size(conn)
    if first is None:
        return "no frame at the expected size", None
    pixels, first_id, fmt = first
    started, last_id, count = time.monotonic(), first_id, 0
    while time.monotonic() - started < seconds:
        got = grab(conn, 1.0)
        if got is not None:
            last_id, count = got[1], count + 1
    elapsed = time.monotonic() - started
    rate = (last_id - first_id) / elapsed if elapsed else 0.0
    shape = "x".join(str(n) for n in pixels.shape[1::-1])
    bad = {k: v - before.get(k, 0) for k, v in STATUS.items() if k != "COMPLETE"}
    bad = {k: v for k, v in bad.items() if v}
    mean = float(np.mean(pixels))
    return (
        f"{shape} {fmt} mean {mean:7.2f} max {int(np.max(pixels))}; "
        f"{rate:5.1f} fps by frame id, {count / elapsed:5.1f} grabbed/s"
        + (f"; incomplete {bad}" if bad else "")
    ), mean


def try_write(conn, name: str, value) -> str:
    setter = conn.set_enum if isinstance(value, str) else conn.set_int
    started = time.monotonic()
    try:
        setter(name, value)
    except DeviceError as exc:
        return f"FAILED ({exc})"
    return f"ok in {time.monotonic() - started:.3f} s"


def paused_write(camera, conn, values: dict) -> str:
    """Write with acquisition stopped, timing the stop-write-start cycle and
    the first frame at the new size."""
    started = time.monotonic()
    results = []
    with camera._acquisition_paused():
        for name, value in values.items():
            results.append(f"{name}={value} {try_write(conn, name, value)}")
    resumed = time.monotonic()
    first, took = frame_at_size(conn, 5.0)
    cycle = resumed - started
    tail = "no frame at the new size" if first is None else f"first frame +{took:.3f} s"
    return f"{'; '.join(results) or 'restart only'}; stop/start {cycle:.3f} s, {tail}"


def apply(camera, conn, values: dict, indent: str = "  ") -> None:
    """Try each write while streaming, then check frames come at the new size.
    Redo refused writes, or restart a stalled stream, with acquisition paused."""
    refused = {}
    for name, value in values.items():
        result = try_write(conn, name, value)
        print(f"{indent}streaming {name}={value}: {result}")
        if result.startswith("FAILED") or read(conn, name) != value:
            refused[name] = value
    if refused:
        print(f"{indent}paused: {paused_write(camera, conn, refused)}")
        return
    first, took = frame_at_size(conn)
    if first is not None:
        print(f"{indent}stream kept going: frame at the new size +{took:.3f} s")
        return
    print(f"{indent}accepted while streaming, but no frame at the new size in 3 s")
    print(f"{indent}restart: {paused_write(camera, conn, {})}")


def snapped(low: int, high: int, inc: int, value: int) -> int:
    return max(low, min(high, low + (value - low) // inc * inc))


def snapshot(conn) -> dict:
    names = (*GEOMETRY, *REDUCTION, *SELECTORS, LIMIT)
    state = {}
    for name in names:
        if conn.has(name) and conn.is_readable(name):
            try:
                state[name] = read(conn, name)
            except DeviceError:
                pass
    return state


def restore(camera, conn, state: dict) -> None:
    # Reduction first (it changes the size limits), then offsets down, sizes
    # back, and offsets to where they were.
    order = [n for n in (*SELECTORS, *REDUCTION) if n in state]
    with camera._acquisition_paused():
        for name in order:
            if conn.is_writable(name) and read(conn, name) != state[name]:
                try_write(conn, name, state[name])
        changed = [n for n in GEOMETRY if n in state and read(conn, n) != state[n]]
        for name in ("OffsetX", "OffsetY"):
            if name in changed:
                try_write(conn, name, 0)
        for name in GEOMETRY:
            if name in changed:
                print(
                    f"  restore {name}={state[name]}: {try_write(conn, name, state[name])}"
                )
        # Geometry changes can move the throughput limit, and the camera keeps
        # it across sessions, so it goes back last.
        if LIMIT in state and read(conn, LIMIT) != state[LIMIT]:
            print(
                f"  restore {LIMIT}={state[LIMIT]}: {try_write(conn, LIMIT, state[LIMIT])}"
            )
    after = snapshot(conn)
    wrong = {k: (v, after.get(k)) for k, v in state.items() if after.get(k) != v}
    print(f"  restored {'everything' if not wrong else f'all but {wrong}'}")
    first, took = frame_at_size(conn, 5.0)
    print(f"  frames after restore: {'none' if first is None else f'+{took:.3f} s'}")


def shift(a: np.ndarray, b: np.ndarray) -> tuple[int, int]:
    """How far the content of b moved relative to a, by phase correlation on a
    central patch: (dx, dy) in pixels."""
    h, w = min(a.shape[0], b.shape[0], 512), min(a.shape[1], b.shape[1], 512)

    def patch(img):
        y0, x0 = (img.shape[0] - h) // 2, (img.shape[1] - w) // 2
        p = img[y0 : y0 + h, x0 : x0 + w].astype(np.float64)
        return (p - p.mean()) * np.outer(np.hanning(h), np.hanning(w))

    fa, fb = np.fft.fft2(patch(a)), np.fft.fft2(patch(b))
    cross = fa * np.conj(fb)
    corr = np.fft.ifft2(cross / (np.abs(cross) + 1e-9)).real
    dy, dx = np.unravel_index(int(np.argmax(corr)), corr.shape)
    dy = dy - h if dy > h // 2 else dy
    dx = dx - w if dx > w // 2 else dx
    return int(dx), int(dy)


def check_roi(camera, conn) -> None:
    print("\nTask 1.4: ROI while streaming (acquisition is running)")
    text, _ = measure(conn)
    print(f"  baseline: {text}; {rate_note(conn)}")
    # Half the size, so the offsets have room to move.
    small = {}
    for name in ("Width", "Height"):
        low, high, inc = conn.int_range(name)
        small[name] = snapped(low, high, inc, conn.get_int(name) // 2)
    apply(camera, conn, small)
    text, _ = measure(conn)
    print(f"  after size change: {text}; {rate_note(conn)}")

    for name in ("OffsetX", "OffsetY"):
        if not conn.has(name):
            print(f"  {name} absent")
            continue
        low, high, inc = conn.int_range(name)
        current = conn.get_int(name)
        step = max(inc, (32 // inc) * inc)
        target = current + step if current + step <= high else low
        print(f"  {name} range {low}..{high} step {inc}, at {current}")
        before, _ = frame_at_size(conn)
        apply(camera, conn, {name: target})
        after, _ = frame_at_size(conn)
        if before is not None and after is not None:
            dx, dy = shift(before[0], after[0])
            print(
                f"  image moved dx={dx} dy={dy} for {name} {current}->{target} "
                f"(offset moved {target - current})"
            )
        text, _ = measure(conn, 1.0)
        print(f"  after {name}: {text}")
    dump(conn, GEOMETRY)


def selector_values(conn, selector: str) -> list:
    if not (conn.has(selector) and conn.is_readable(selector)):
        return [None]
    return list(conn.enum_entries(selector))


def factors(conn, name: str) -> list[int]:
    if not (conn.has(name) and conn.is_writable(name)):
        return []
    low, high, inc = conn.int_range(name)
    return [f for f in (1, 2, 3, 4, 8) if low <= f <= high and (f - low) % inc == 0]


def check_reduction(camera, conn, kind: str) -> None:
    for sel in selector_values(conn, f"{kind}Selector"):
        label = kind if sel is None else f"{kind} ({kind}Selector={sel})"
        print(f"\nTask 1.5: {label}")
        if sel is not None:
            print(f"  select: {try_write(conn, f'{kind}Selector', sel)}")
        dump(conn, [n for n in REDUCTION if n.startswith(kind)])
        h, v = f"{kind}Horizontal", f"{kind}Vertical"
        modes = [None]
        mode_node = f"{kind}HorizontalMode"
        if conn.has(mode_node) and conn.is_writable(mode_node):
            modes = list(conn.enum_entries(mode_node))
        # Some cameras (the CM3) expose only one axis as writable and link
        # the other to it; drive the writable ones and report both.
        axes = [n for n in (h, v) if factors(conn, n)]
        for mode in modes:
            base = None
            for f in factors(conn, axes[0]) if axes else []:
                values = {n: f for n in axes if f in factors(conn, n)}
                if mode is not None:
                    values[mode_node] = mode
                    if conn.has(f"{kind}VerticalMode"):
                        values[f"{kind}VerticalMode"] = mode
                print(f"  {', '.join(f'{n}={x}' for n, x in values.items())}")
                apply(camera, conn, values, indent="    ")
                text, mean = measure(conn)
                if f == 1:
                    base = mean
                ratio = (
                    f"; mean x{mean / base:.2f} vs 1x1"
                    if base and mean is not None and f != 1
                    else ""
                )
                print(f"    {text}{ratio}")
                print(f"    {rate_note(conn)}")
                print(f"    {h}={conn.get_int(h)} {v}={conn.get_int(v)}")
                print(
                    f"    size now {describe(conn, 'Width')} x {describe(conn, 'Height')}"
                )
            if axes:
                back = {n: 1 for n in axes}
                print(f"  back to 1: {paused_write(camera, conn, back)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("backend", choices=("spinnaker", "aravis", "gentl"))
    parser.add_argument("--serial")
    parser.add_argument("--producer", help="gentl: path to the .cti")
    parser.add_argument("--exposure", type=float, help="exposure time in seconds")
    args = parser.parse_args()

    camera = open_camera(args)
    conn = camera._connection
    state = snapshot(conn)
    try:
        info = camera.info
        print(f"{info.model} {info.serial_number}, firmware {info.firmware_version}")
        print(
            f"backend {args.backend}; host {platform.platform()}, "
            f"python {platform.python_version()}"
        )
        autos = {}
        for name in ("ExposureAuto", "GainAuto"):
            if name in camera.settings():
                autos[name] = camera.setting(name).value
        print(f"as found: {autos}")
        if args.exposure is not None:
            # Fixed exposure and gain, so mean levels compare across modes.
            for name in autos:
                camera.set_setting(name, "Off")
            camera.set_setting(EXPOSURE_TIME, args.exposure)
        print(f"exposure {camera.setting(EXPOSURE_TIME).value} s")
        print("\nnodes as found (acquisition running):")
        dump(conn, (*GEOMETRY, *SIZE_LIMITS, *SELECTORS, *REDUCTION, *RATE))
        for check in (
            lambda: check_roi(camera, conn),
            lambda: check_reduction(camera, conn, "Binning"),
            lambda: check_reduction(camera, conn, "Decimation"),
        ):
            try:
                check()
            except DeviceError as exc:
                print(f"  check stopped: {exc}")
            restore(camera, conn, state)
        print(f"\nbuffer statuses: {dict(STATUS)}")
        for name, value in autos.items():
            camera.set_setting(name, value)
    except BaseException:
        print("\nrestoring the geometry as found")
        restore(camera, conn, state)
        raise
    finally:
        camera.close()


if __name__ == "__main__":
    main()
