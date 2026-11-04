"""ROI, binning and decimation discovery on a GenICam camera (Tasks 1.4, 1.5).

    python conductor/tracks/preview-mode_20261102/genicam_geometry.py spinnaker
    python conductor/tracks/preview-mode_20261102/genicam_geometry.py aravis \
        --serial 4108912345
    python conductor/tracks/preview-mode_20261102/genicam_geometry.py gentl \
        --producer /opt/ids-peak/lib/ids/cti/ids_u3vgentl.cti

Point the camera at an evenly lit, mid-grey scene (nothing saturated), so
mean levels show whether binning averages or sums. The camera's ROI, binning
and decimation are put back as found at the end, even on failure. It never
touches TLParamsLocked. Paste the output back for the hardware docs.
"""

import argparse
import platform
import time

import numpy as np

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
    "AcquisitionFrameRate",
    "AcquisitionResultingFrameRate",
    "DeviceLinkThroughputLimit",
    "DeviceLinkThroughputLimitMode",
    "PixelFormat",
)


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
    for kind, probe in (("int", conn.int_range), ("float", conn.float_range)):
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
    extra = {
        "enum": lambda: f" entries={list(conn.enum_entries(name))}",
        "int": lambda: f" min/max/inc={conn.int_range(name)}",
        "float": lambda: f" min/max={conn.float_range(name)}",
        "string": lambda: "",
    }[kind]()
    return f"{access} {kind} {value!r}{extra}"


def dump(conn, names, indent: str = "  ") -> None:
    for name in names:
        print(f"{indent}{name:<32} {describe(conn, name)}")


def grab(conn, timeout: float = 2.0):
    """The next complete frame's pixels and id, or None on a timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            buffer = conn.grab(max(0.05, deadline - time.monotonic()))
        except DeviceTimeout:
            return None
        try:
            if buffer.status().name == "COMPLETE":
                return buffer.pixels(), buffer.frame_id(), buffer.pixel_format()
        finally:
            buffer.release()
    return None


def measure(conn, seconds: float = 2.0) -> str:
    first = grab(conn)
    if first is None:
        return "no frame"
    pixels, first_id, fmt = first
    started, last_id, count = time.monotonic(), first_id, 0
    while time.monotonic() - started < seconds:
        got = grab(conn, 1.0)
        if got is not None:
            last_id, count = got[1], count + 1
    elapsed = time.monotonic() - started
    rate = (last_id - first_id) / elapsed if elapsed else 0.0
    shape = "x".join(str(n) for n in pixels.shape[1::-1])
    return (
        f"{shape} {fmt} mean {np.mean(pixels):8.2f} max {int(np.max(pixels))}; "
        f"{rate:5.1f} fps by frame id, {count / elapsed:5.1f} grabbed/s"
    )


def try_write(conn, name: str, value) -> str:
    setter = conn.set_enum if isinstance(value, str) else conn.set_int
    started = time.monotonic()
    try:
        setter(name, value)
    except DeviceError as exc:
        return f"FAILED ({exc})"
    return f"ok in {time.monotonic() - started:.3f} s"


def paused_write(camera, conn, values: dict) -> str:
    """Write with acquisition stopped, timing the stop-write-start-frame cycle."""
    started = time.monotonic()
    results = []
    with camera._acquisition_paused():
        for name, value in values.items():
            results.append(f"{name}={value} {try_write(conn, name, value)}")
    resumed = time.monotonic()
    first = grab(conn, 5.0)
    done = time.monotonic()
    tail = "no frame" if first is None else f"first frame +{done - resumed:.3f} s"
    return f"{'; '.join(results)}; cycle {done - started:.3f} s, {tail}"


def apply(camera, conn, values: dict, indent: str = "  ") -> None:
    """Try each write while streaming; redo the ones that fail with
    acquisition paused."""
    failed = {}
    for name, value in values.items():
        result = try_write(conn, name, value)
        print(f"{indent}streaming {name}={value}: {result}")
        if result.startswith("FAILED") or read(conn, name) != value:
            failed[name] = value
    if failed:
        print(f"{indent}paused: {paused_write(camera, conn, failed)}")


def snapped(low: int, high: int, inc: int, value: int) -> int:
    return max(low, min(high, low + (value - low) // inc * inc))


def snapshot(conn) -> dict:
    names = (*GEOMETRY, *REDUCTION, *SELECTORS)
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
            if read(conn, name) != state[name]:
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
    after = snapshot(conn)
    wrong = {k: (v, after.get(k)) for k, v in state.items() if after.get(k) != v}
    print(f"  restored {'everything' if not wrong else f'all but {wrong}'}")


def check_roi(camera, conn) -> None:
    print("\nTask 1.4: ROI while streaming (acquisition is running)")
    print(f"  baseline: {measure(conn)}")
    # Half the size, so the offsets have room to move.
    small = {}
    for name in ("Width", "Height"):
        low, high, inc = conn.int_range(name)
        small[name] = snapped(low, high, inc, conn.get_int(name) // 2)
    apply(camera, conn, small)
    print(f"  after size change: {measure(conn)}")

    for name in ("OffsetX", "OffsetY"):
        if not conn.has(name):
            print(f"  {name} absent")
            continue
        low, high, inc = conn.int_range(name)
        current = conn.get_int(name)
        target = current + 4 * inc if current + 4 * inc <= high else low
        print(f"  {name} range {low}..{high} step {inc}, at {current}")
        apply(camera, conn, {name: target})
        print(f"  after {name}: {measure(conn, 1.0)}")
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
    selector = f"{kind}Selector"
    for sel in selector_values(conn, selector):
        label = f"{kind}" if sel is None else f"{kind} ({selector}={sel})"
        print(f"\nTask 1.5: {label}")
        if sel is not None:
            print(f"  {try_write(conn, selector, sel)}")
        names = [n for n in REDUCTION if n.startswith(kind)]
        dump(conn, [*names, "AcquisitionResultingFrameRate"])
        h, v = f"{kind}Horizontal", f"{kind}Vertical"
        modes = [None]
        mode_node = f"{kind}HorizontalMode"
        if conn.has(mode_node) and conn.is_readable(mode_node):
            modes = list(conn.enum_entries(mode_node))
        for mode in modes:
            for f in factors(conn, h):
                values = {h: f}
                if f in factors(conn, v):
                    values[v] = f
                if mode is not None:
                    values[mode_node] = mode
                    if conn.has(f"{kind}VerticalMode"):
                        values[f"{kind}VerticalMode"] = mode
                print(f"  {', '.join(f'{n}={x}' for n, x in values.items())}")
                apply(camera, conn, values, indent="    ")
                rate = describe(conn, "AcquisitionResultingFrameRate")
                print(f"    {measure(conn)}; resulting {rate}")
                print(
                    f"    size now {describe(conn, 'Width')} x {describe(conn, 'Height')}"
                )
            if factors(conn, h):
                paused_write(
                    camera, conn, {h: 1, **({v: 1} if factors(conn, v) else {})}
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("backend", choices=("spinnaker", "aravis", "gentl"))
    parser.add_argument("--serial")
    parser.add_argument("--producer", help="gentl: path to the .cti")
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
    except BaseException:
        print("\nrestoring the geometry as found")
        restore(camera, conn, state)
        raise
    finally:
        camera.close()


if __name__ == "__main__":
    main()
