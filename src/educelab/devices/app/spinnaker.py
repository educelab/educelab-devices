"""``el-devices spinnaker``: list Spinnaker cameras and benchmark their captures."""

import argparse
import statistics
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

# Imported where it's used, so other subcommands don't load the driver.
if TYPE_CHECKING:
    from educelab.devices.cameras import Frame, Setting
    from educelab.devices.cameras.spinnaker import SpinnakerCamera
    from educelab.devices.settings import SettingValue

DEFAULT_FRAMES = 20
DEFAULT_TIMEOUT_S = 5.0


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    parser = subparsers.add_parser(
        "spinnaker",
        help="list Spinnaker cameras and benchmark their captures",
        description="List the attached Spinnaker cameras, then open them all at once "
        "(so they all stream, as on a rig) and capture frames from every camera "
        "together, each no older than the moment it was requested. Reports capture "
        "latency, frame age, dropped frames, and the bandwidth each camera streams.",
    )
    parser.add_argument(
        "--serial",
        action="append",
        help="only use this camera; repeat for several (default: every camera)",
    )
    parser.add_argument(
        "--frames",
        type=_count,
        default=DEFAULT_FRAMES,
        help=f"captures per camera (default: {DEFAULT_FRAMES}); 0 only lists cameras",
    )
    parser.add_argument(
        "--exposure",
        type=_exposure,
        help="exposure time in seconds, or 'min' for the camera's shortest, with auto "
        "exposure off (default: leave it)",
    )
    parser.add_argument(
        "--gain",
        type=float,
        help="gain in dB, with auto gain off (default: leave it)",
    )
    parser.add_argument("--pixel-format", help="e.g. Mono8, Mono16, or RGB8")
    parser.add_argument(
        "--throughput-limit",
        type=_positive,
        metavar="MBPS",
        help="cap each camera's USB bandwidth at this many MB/s (10^6 bytes), e.g. "
        "80 for five cameras sharing one USB 3 link (default: leave it)",
    )
    parser.add_argument(
        "--timeout",
        type=_positive,
        default=DEFAULT_TIMEOUT_S,
        help=f"seconds allowed per capture (default: {DEFAULT_TIMEOUT_S:g})",
    )
    parser.set_defaults(func=run)


def _count(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, not {value}")
    return value


def _exposure(text: str) -> float | str:
    return "min" if text.strip().lower() == "min" else _positive(text)


def _positive(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not value > 0:
        raise argparse.ArgumentTypeError(f"must be positive, not {value}")
    return value


@dataclass
class _Results:
    """One camera's captures."""

    latencies_ms: list[float] = field(default_factory=list)
    ages_ms: list[float] = field(default_factory=list)
    frames: list["Frame"] = field(default_factory=list)
    stale: int = 0
    incomplete: int = 0
    missing_trailer: int = 0
    failures: list[str] = field(default_factory=list)


def run(args: argparse.Namespace) -> int:
    from educelab.devices.cameras.spinnaker import SpinnakerCamera, SpinnakerSystem

    try:
        with SpinnakerSystem() as system:
            print(f"Spinnaker SDK {system.library_version}")
            found = system.discover()
    except ImportError as exc:
        print(f"el-devices: {exc}", file=sys.stderr)
        return 1
    print(f"{len(found)} camera(s):")
    for info in found:
        print(f"  {info.serial_number or '(no serial)'}  {info.model}")
    serials = args.serial or [i.serial_number for i in found if i.serial_number]
    if not args.frames or not serials:
        return 0

    cameras = [SpinnakerCamera(serial) for serial in serials]
    opened: list[SpinnakerCamera] = []
    try:
        for camera in cameras:
            camera.open()
            opened.append(camera)
            problem = _set_up(camera, args)
            if problem:
                print(f"el-devices: {problem}", file=sys.stderr)
                return 1
            _describe(camera)
        results = _benchmark(opened, args.frames, args.timeout)
    finally:
        for camera in reversed(opened):
            camera.close()
    failed = 0
    for camera in opened:
        failed += _report(camera, results[camera], args.frames)
    return 1 if failed else 0


def _describe(camera: "SpinnakerCamera") -> None:
    from educelab.devices.cameras import EXPOSURE_TIME, GAIN, PIXEL_FORMAT

    info = camera.info
    settings = camera.settings()
    exposure = settings[EXPOSURE_TIME].value
    exposure_text = f"{exposure * 1000:.3g} ms" if isinstance(exposure, float) else "?"
    gain = settings[GAIN].value if GAIN in settings else None
    offset = camera.timestamp_offset
    print(
        f"{info.serial_number}: {info.model}, firmware {info.firmware_version}, "
        f"{settings[PIXEL_FORMAT].value}, exposure {exposure_text} "
        f"({settings['ExposureAuto'].value}), gain {gain} dB, "
        f"{_throughput_text(settings)}, "
        f"timestamp node {camera.timestamp_node}, "
        f"offset {'none' if offset is None else f'{offset:.6f} s'}"
    )


def _set_up(camera: "SpinnakerCamera", args: argparse.Namespace) -> str | None:
    """Apply the requested settings; returns what went wrong, if anything.

    The throughput limit goes before exposure, because it lowers the frame rate,
    which raises the longest exposure the camera allows.
    """
    from educelab.devices.cameras import EXPOSURE_TIME, GAIN, PIXEL_FORMAT
    from educelab.devices.cameras.spinnaker import THROUGHPUT_LIMIT

    if args.pixel_format and (
        problem := _apply(camera, PIXEL_FORMAT, args.pixel_format)
    ):
        return problem
    if args.throughput_limit is not None:
        if THROUGHPUT_LIMIT not in camera.settings():
            return (
                f"{camera.info.serial_number} has no DeviceLinkThroughputLimit, "
                "so it can't be capped"
            )
        if problem := _apply(camera, THROUGHPUT_LIMIT, args.throughput_limit):
            return problem
    if args.exposure is not None:
        camera.set_setting("ExposureAuto", "Off")
        exposure = args.exposure
        if exposure == "min":
            exposure = camera.settings()[EXPOSURE_TIME].minimum
            if exposure is None:
                return f"{camera.info.serial_number} reports no minimum exposure"
        if problem := _apply(camera, EXPOSURE_TIME, exposure):
            return problem
    if args.gain is not None:
        camera.set_setting("GainAuto", "Off")
        if problem := _apply(camera, GAIN, args.gain):
            return problem
    return None


def _apply(camera: "SpinnakerCamera", name: str, value: "SettingValue") -> str | None:
    """Write one setting; on a bad value, say which camera and what it accepts."""
    try:
        camera.set_setting(name, value)
    except ValueError as exc:
        setting = camera.settings().get(name)
        problem = f"{camera.info.serial_number}: {exc}"
        if setting and setting.minimum is not None and setting.maximum is not None:
            unit = f" {setting.unit}" if setting.unit else ""
            problem += f" (valid: {setting.minimum:g} to {setting.maximum:g}{unit})"
        return problem
    return None


def _throughput_text(settings: Mapping[str, "Setting"]) -> str:
    from educelab.devices.cameras.spinnaker import THROUGHPUT_LIMIT

    limit = settings.get(THROUGHPUT_LIMIT)
    if limit is None:
        text = "no throughput limit node"
    elif limit.value is None:
        text = "throughput limit off"
    else:
        text = f"throughput limit {limit.value:g} MB/s"
    rate = settings.get("resulting_frame_rate")
    if rate is not None and rate.value is not None:
        text += f", resulting {rate.value:.1f} frames/s"
    return text


def _benchmark(
    cameras: list["SpinnakerCamera"], frames: int, timeout: float
) -> dict["SpinnakerCamera", _Results]:
    """Capture from every camera at once, ``frames`` times, as a rig would."""
    results = {camera: _Results() for camera in cameras}

    def capture(camera: "SpinnakerCamera", requested: datetime) -> None:
        result = results[camera]
        start = time.perf_counter()
        try:
            frame = camera.capture_frame(not_before=requested, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            # Anything, even a driver bug: an uncaught exception would only print
            # a traceback from this thread and drop the capture from the report.
            result.failures.append(f"{type(exc).__name__}: {exc}")
            return
        result.latencies_ms.append((time.perf_counter() - start) * 1000)
        result.ages_ms.append((frame.timestamp - requested).total_seconds() * 1000)
        result.frames.append(frame)
        result.stale += frame.metadata["stale_frames"]
        result.incomplete += frame.metadata["incomplete_frames"]
        result.missing_trailer += bool(frame.metadata["missing_trailer"])

    print(f"Capturing {frames} frame(s) from {len(cameras)} camera(s) at once...")
    trial_ms = []
    for _ in range(frames):
        requested = datetime.now(UTC)
        start = time.perf_counter()
        threads = [
            threading.Thread(target=capture, args=(camera, requested))
            for camera in cameras
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        trial_ms.append((time.perf_counter() - start) * 1000)
    print(f"all cameras per capture: {_stats(trial_ms)}")
    return results


def _report(camera: "SpinnakerCamera", result: _Results, frames: int) -> int:
    """Print one camera's results; returns the number of failed captures."""
    serial = camera.info.serial_number
    print(f"{serial}: {len(result.frames)}/{frames} captured")
    if result.latencies_ms:
        print(f"  latency {_stats(result.latencies_ms)}")
        print(f"  frame age {_stats(result.ages_ms)} (timestamp minus request)")
    print(
        f"  dropped {result.stale} stale, {result.incomplete} incomplete; "
        f"{result.missing_trailer} accepted with a missing trailer"
    )
    streamed = _streaming_rate(result.frames)
    if streamed:
        fps, mb_per_s = streamed
        print(f"  streaming ~{fps:.1f} frames/s, ~{mb_per_s:.1f} MB/s over USB")
    for failure in result.failures:
        print(f"  failed: {failure}")
    return len(result.failures)


def _streaming_rate(frames: list["Frame"]) -> tuple[float, float] | None:
    """Frames per second and MB/s the camera sent, from frame IDs and timestamps.

    Frame IDs count every frame the camera streamed, including the ones newest-only
    buffering discarded (assumed; see docs/hardware/spinnaker.md).
    """
    if len(frames) < 2:
        return None
    first, last = frames[0], frames[-1]
    elapsed = (
        (last.metadata["device_timestamp_ns"]) - (first.metadata["device_timestamp_ns"])
    )
    sent = last.metadata["frame_id"] - first.metadata["frame_id"]
    if elapsed <= 0 or sent <= 0:
        return None
    fps = sent / (elapsed / 1e9)
    return fps, fps * last.pixels.nbytes / 1e6


def _stats(values_ms: list[float]) -> str:
    return (
        f"median {statistics.median(values_ms):.1f} ms "
        f"(mean {statistics.mean(values_ms):.1f}, min {min(values_ms):.1f}, "
        f"max {max(values_ms):.1f})"
    )
