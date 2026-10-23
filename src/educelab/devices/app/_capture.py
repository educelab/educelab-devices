"""The capture benchmark shared by ``el-devices spinnaker`` and ``el-devices genicam``.

Both open every chosen camera at once (so they all stream, as on a rig), capture
from them together, and report latency, exposure start, dropped frames and USB
bandwidth.
"""

import argparse
import statistics
import struct
import sys
import threading
import time
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

# Imported where it's used, so other subcommands don't load the drivers.
if TYPE_CHECKING:
    from educelab.devices.cameras import Frame, Setting
    from educelab.devices.cameras._genicam import GenICamCamera
    from educelab.devices.device import DeviceInfo
    from educelab.devices.settings import SettingValue

DEFAULT_FRAMES = 20
DEFAULT_TIMEOUT_S = 5.0

_TIMESTAMP_SOURCES = {"latch": "from the latch", "arrival": "from frame arrivals"}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the camera choice, settings and benchmark options."""
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
        "--trigger",
        choices=("off", "software"),
        default="off",
        help="'software' exposes a frame only when a capture asks for one, firing "
        "every camera's trigger together (default: off, free-running)",
    )
    parser.add_argument(
        "--save-missing-trailer",
        type=Path,
        metavar="DIR",
        help="save frames accepted with a missing trailer as PNGs in DIR, with one "
        "complete frame per camera for comparison",
    )
    parser.add_argument(
        "--timeout",
        type=_positive,
        default=DEFAULT_TIMEOUT_S,
        help=f"seconds allowed per capture (default: {DEFAULT_TIMEOUT_S:g})",
    )


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


def list_cameras(found: Sequence["DeviceInfo"]) -> list[str]:
    """Print the cameras found; returns their serial numbers, without repeats."""
    print(f"{len(found)} camera(s):")
    for info in found:
        print(f"  {info.serial_number or '(no serial)'}  {info.model}")
    # Two GenTL producers can both list one camera.
    return list(dict.fromkeys(i.serial_number for i in found if i.serial_number))


@dataclass
class _Results:
    """One camera's captures."""

    latencies_ms: list[float] = field(default_factory=list)
    starts_ms: list[float] = field(default_factory=list)
    # (frame ID, device timestamp in ns), for the streaming rate; pixels aren't kept.
    stamps: list[tuple[int, int]] = field(default_factory=list)
    frame_bytes: int = 0
    bit_depth: int | None = None
    dated_by_trigger: int = 0
    saved: int = 0
    stale: int = 0
    incomplete: int = 0
    retriggers: int = 0
    missing_trailer: int = 0
    failures: list[str] = field(default_factory=list)


def run_benchmark(cameras: Sequence["GenICamCamera"], args: argparse.Namespace) -> int:
    """Open ``cameras``, apply the settings in ``args``, benchmark and report.

    Returns the exit status: 1 if a setting was refused or a capture failed.
    """
    opened: list[GenICamCamera] = []
    try:
        for camera in cameras:
            camera.open()
            opened.append(camera)
            problem = _set_up(camera, args)
            if problem:
                print(f"el-devices: {problem}", file=sys.stderr)
                return 1
            _describe(camera)
        if args.save_missing_trailer:
            args.save_missing_trailer.mkdir(parents=True, exist_ok=True)
        results = _benchmark(
            opened,
            args.frames,
            args.timeout,
            trigger=args.trigger == "software",
            save_dir=args.save_missing_trailer,
        )
    finally:
        for camera in reversed(opened):
            camera.close()
    failed = 0
    for camera in opened:
        failed += _report(camera, results[camera], args.frames)
    return 1 if failed else 0


def _describe(camera: "GenICamCamera") -> None:
    from educelab.devices.cameras import EXPOSURE_TIME, GAIN, PIXEL_FORMAT

    info = camera.info
    settings = camera.settings()
    exposure = settings[EXPOSURE_TIME].value if EXPOSURE_TIME in settings else None
    exposure_text = f"{exposure * 1000:.3g} ms" if isinstance(exposure, float) else "?"
    if "ExposureAuto" in settings:
        exposure_text += f" ({settings['ExposureAuto'].value})"
    gain = settings[GAIN].value if GAIN in settings else None
    pixel_format = settings[PIXEL_FORMAT].value if PIXEL_FORMAT in settings else "?"
    offset = camera.timestamp_offset
    offset_text = "none" if offset is None else f"{offset:.6f} s"
    if camera.timestamp_source:
        offset_text += f" {_TIMESTAMP_SOURCES[camera.timestamp_source]}"
    print(
        f"{info.serial_number}: {info.model}, firmware {info.firmware_version}, "
        f"{pixel_format}, exposure {exposure_text}, gain {gain} dB, "
        f"{_throughput_text(settings)}, "
        f"trigger {settings['trigger'].value if 'trigger' in settings else 'none'}, "
        f"timestamp node {camera.timestamp_node}, timestamp offset {offset_text}"
    )


def _set_up(camera: "GenICamCamera", args: argparse.Namespace) -> str | None:
    """Apply the requested settings; returns what went wrong, if anything.

    The throughput limit goes before exposure, because it lowers the frame rate,
    which raises the longest exposure the camera allows.
    """
    from educelab.devices.cameras import EXPOSURE_TIME, GAIN, PIXEL_FORMAT
    from educelab.devices.cameras._genicam import THROUGHPUT_LIMIT, TRIGGER

    serial = camera.info.serial_number
    if args.trigger == "software":
        if TRIGGER not in camera.settings():
            return f"{serial} has no software trigger"
        if problem := _apply(camera, TRIGGER, "Software"):
            return problem
    if args.pixel_format and (
        problem := _apply(camera, PIXEL_FORMAT, args.pixel_format)
    ):
        return problem
    if args.throughput_limit is not None:
        if THROUGHPUT_LIMIT not in camera.settings():
            return f"{serial} has no DeviceLinkThroughputLimit, so it can't be capped"
        if problem := _apply(camera, THROUGHPUT_LIMIT, args.throughput_limit):
            return problem
    if args.exposure is not None:
        settings = camera.settings()
        if EXPOSURE_TIME not in settings:
            return f"{serial} has no exposure time"
        if "ExposureAuto" in settings:
            camera.set_setting("ExposureAuto", "Off")
        exposure = args.exposure
        if exposure == "min":
            exposure = camera.settings()[EXPOSURE_TIME].minimum
            if exposure is None:
                return f"{serial} reports no minimum exposure"
        if problem := _apply(camera, EXPOSURE_TIME, exposure):
            return problem
    if args.gain is not None:
        settings = camera.settings()
        if GAIN not in settings:
            return f"{serial} has no gain"
        if "GainAuto" in settings:
            camera.set_setting("GainAuto", "Off")
        if problem := _apply(camera, GAIN, args.gain):
            return problem
    return None


def _apply(camera: "GenICamCamera", name: str, value: "SettingValue") -> str | None:
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
    from educelab.devices.cameras._genicam import THROUGHPUT_LIMIT

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
    cameras: list["GenICamCamera"],
    frames: int,
    timeout: float,
    *,
    trigger: bool = False,
    save_dir: Path | None = None,
) -> dict["GenICamCamera", _Results]:
    """Capture from every camera at once, ``frames`` times, as a rig would.

    With ``trigger``, every camera's software trigger is fired first, then the
    frames are collected.
    """
    results = {camera: _Results() for camera in cameras}

    def capture(camera: "GenICamCamera", requested: datetime, index: int) -> None:
        result = results[camera]
        # A camera that can't be timed refuses not_before; capture it untimed.
        timed = camera.timestamp_offset is not None
        start = time.perf_counter()
        try:
            frame = camera.capture_frame(
                not_before=requested if timed else None, timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001
            # Anything, even a driver bug: an uncaught exception would only print
            # a traceback from this thread and drop the capture from the report.
            result.failures.append(f"{type(exc).__name__}: {exc}")
            return
        result.latencies_ms.append((time.perf_counter() - start) * 1000)
        if timed:
            starts = (frame.timestamp - requested).total_seconds() * 1000
            result.starts_ms.append(starts)
        result.stamps.append(
            (frame.metadata["frame_id"], frame.metadata["device_timestamp_ns"])
        )
        result.bit_depth = frame.metadata.get("bit_depth")
        # What crossed the link: 10-bit pixels arrive packed, but unpack to uint16.
        depth = result.bit_depth or 8 * frame.pixels.itemsize
        result.frame_bytes = frame.pixels.size * depth // 8
        result.dated_by_trigger += frame.metadata.get("timestamp_source") == "trigger"
        result.stale += frame.metadata["stale_frames"]
        result.incomplete += frame.metadata["incomplete_frames"]
        result.retriggers += frame.metadata["retriggers"]
        missing = bool(frame.metadata["missing_trailer"])
        result.missing_trailer += missing
        if save_dir is not None and (missing or not result.saved):
            name = f"missing-trailer-{index:04d}" if missing else "complete"
            _write_png(save_dir / f"{camera.info.serial_number}-{name}.png", frame)
            result.saved += 1

    from educelab.devices.cameras._genicam import host_time

    print(f"Capturing {frames} frame(s) from {len(cameras)} camera(s) at once...")
    trial_ms = []
    for index in range(frames):
        # Not datetime.now(): on Windows before Python 3.13 it ticks every 15.6 ms.
        requested = datetime.fromtimestamp(host_time(), UTC)
        start = time.perf_counter()
        if trigger:
            for camera in cameras:
                try:
                    camera.trigger()
                except Exception as exc:  # noqa: BLE001
                    # The capture fires its own trigger, or reports the failure.
                    print(f"{camera.info.serial_number}: trigger failed: {exc}")
        threads = [
            threading.Thread(target=capture, args=(camera, requested, index))
            for camera in cameras
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        trial_ms.append((time.perf_counter() - start) * 1000)
    print(f"all cameras per capture: {_stats(trial_ms)}")
    return results


def _report(camera: "GenICamCamera", result: _Results, frames: int) -> int:
    """Print one camera's results; returns the number of failed captures."""
    serial = camera.info.serial_number
    print(f"{serial}: {len(result.stamps)}/{frames} captured")
    if result.bit_depth is not None:
        print(f"  {result.bit_depth}-bit pixels")
    if result.latencies_ms:
        print(f"  latency {_stats(result.latencies_ms)}")
    if result.starts_ms:
        print(f"  exposure start {_stats(result.starts_ms)} after the request")
    elif camera.timestamp_offset is None:
        print("  frames can't be timed, so captured without not_before")
    print(
        f"  dropped {result.stale} stale, {result.incomplete} incomplete; "
        f"{result.missing_trailer} accepted with a missing trailer"
    )
    if result.retriggers:
        print(f"  fired {result.retriggers} trigger(s) again after no frame came")
    if result.dated_by_trigger:
        print(f"  {result.dated_by_trigger} frame(s) dated by their trigger")
    if result.saved:
        print(f"  saved {result.saved} frame(s)")
    streamed = _streaming_rate(result.stamps, result.frame_bytes)
    if streamed:
        fps, mb_per_s = streamed
        print(f"  streaming ~{fps:.1f} frames/s, ~{mb_per_s:.1f} MB/s over USB")
    for failure in result.failures:
        print(f"  failed: {failure}")
    return len(result.failures)


def _streaming_rate(
    stamps: list[tuple[int, int]], frame_bytes: int
) -> tuple[float, float] | None:
    """Frames per second and MB/s the camera sent, from frame IDs and timestamps.

    Frame IDs count every frame the camera streamed, including the ones newest-only
    buffering discarded (assumed; see docs/hardware/spinnaker.md).
    """
    if len(stamps) < 2:
        return None
    (first_id, first_ns), (last_id, last_ns) = min(stamps), max(stamps)
    elapsed, sent = last_ns - first_ns, last_id - first_id
    if elapsed <= 0 or sent <= 0:
        return None
    fps = sent / (elapsed / 1e9)
    return fps, fps * frame_bytes / 1e6


def _write_png(path: Path, frame: "Frame") -> None:
    """Save a frame as an 8- or 16-bit grayscale or RGB PNG; Bayer frames stay raw."""
    import numpy as np

    pixels = frame.pixels
    if pixels.ndim == 2:
        pixels = pixels[:, :, np.newaxis]
    height, width, channels = pixels.shape
    depth = 16 if pixels.dtype == np.uint16 else 8
    rows = pixels.astype(">u2" if depth == 16 else np.uint8).reshape(height, -1)
    raw = np.hstack([np.zeros((height, 1), np.uint8), rows.view(np.uint8)])

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    color = {1: 0, 3: 2}[channels]
    header = struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw.tobytes()))
        + chunk(b"IEND", b"")
    )


def _stats(values_ms: list[float]) -> str:
    return (
        f"median {statistics.median(values_ms):.1f} ms "
        f"(mean {statistics.mean(values_ms):.1f}, min {min(values_ms):.1f}, "
        f"max {max(values_ms):.1f})"
    )
