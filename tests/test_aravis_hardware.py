"""Aravis tests against real cameras: ``pytest -m hardware tests/test_aravis_hardware.py``.

Needs Aravis 0.8 and PyGObject (docs/hardware/usb3-vision.md). Uses the camera
named by ``ARAVIS_SERIAL``, or the first one attached; the multi-camera test uses
every attached camera. Settings changed by a test are put back afterwards. Several
tests print what they measured, for the hardware doc: run with ``-s``.
"""

import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from educelab.devices.cameras import EXPOSURE_TIME, PIXEL_FORMAT
from educelab.devices.cameras._genicam import (
    RETRIES_BEFORE_REMEASURE,
    THROUGHPUT_LIMIT,
    TRIGGER,
)
from educelab.devices.cameras.aravis import AravisCamera
from educelab.devices.errors import DeviceBusy, DeviceError, DeviceTimeout

pytestmark = pytest.mark.hardware

# Loose on purpose: these catch a broken offset or a wedged stream, not jitter.
OFFSET_SPREAD_S = 0.005
MAX_FRAME_AGE_S = 1.0


def attached() -> list[str]:
    try:
        infos = AravisCamera.discover()
    except ImportError as exc:
        pytest.skip(str(exc))
    serials = [info.serial_number for info in infos if info.serial_number]
    if not serials:
        pytest.skip("no Aravis cameras attached")
    return serials


@pytest.fixture(scope="module")
def serial() -> str:
    return os.environ.get("ARAVIS_SERIAL") or attached()[0]


@pytest.fixture(scope="module")
def camera(serial):
    camera = AravisCamera(serial)
    try:
        camera.open()
    except ImportError as exc:
        pytest.skip(str(exc))
    yield camera
    camera.close()


@pytest.fixture
def restore(camera):
    """Put every writable setting back after the test."""
    saved = {n: s.value for n, s in camera.settings().items() if not s.read_only}
    yield camera
    # Auto modes first, so the values they lock become writable again.
    for name in sorted(saved, key=lambda n: not n.endswith("Auto")):
        value = saved[name]
        if value is not None and not camera.setting(name).read_only:
            camera.set_setting(name, value)


def test_discover_reports_serial_model_and_address(serial):
    infos = {i.serial_number: i for i in AravisCamera.discover()}
    assert serial in infos
    print(f"discovered: {infos[serial]}")
    assert infos[serial].model


def test_info_has_model_and_firmware(camera, serial):
    info = camera.info
    print(f"info: {info}")
    assert info.serial_number == serial
    assert info.model != "Aravis camera"


def test_settings_the_camera_offers(camera):
    # Records which optional features this model has (usb3-vision.md).
    for name, setting in camera.settings().items():
        print(f"{name}: {setting}")
    assert EXPOSURE_TIME in camera.settings()


def test_camera_latches_a_timestamp(camera):
    print(f"timestamp node: {camera.timestamp_node}")
    assert camera.timestamp_node in ("Timestamp", "TimestampLatchValue")
    assert camera.timestamp_offset is not None


def test_timestamp_offset_is_stable(camera):
    offsets = [camera.measure_timestamp_offset() for _ in range(5)]
    assert None not in offsets
    values = [o for o in offsets if o is not None]
    spread = max(values) - min(values)
    print(f"offset spread over 5 measurements: {spread * 1e6:.0f} us")
    assert spread < OFFSET_SPREAD_S


def test_buffer_timestamps_are_the_device_counter(camera):
    # Aravis issue #887: some cameras put 0 in the buffer timestamp.
    frame = camera.capture_frame()
    print(f"device timestamp {frame.metadata['device_timestamp_ns']} ns")
    assert frame.metadata["device_timestamp_ns"] > 0


def test_device_timestamp_against_buffer_arrival(camera):
    # What moment the device stamps, relative to when the host had the whole
    # frame. Settles the rolling-shutter question in usb3-vision.md, Timestamps.
    conn = camera._connection
    exposure = camera.setting(EXPOSURE_TIME).value
    gaps = []
    for _ in range(10):
        buffer = conn.grab(5.0)
        try:
            stamped = camera.timestamp_offset + buffer.device_timestamp_ns() / 1e9
            arrived = buffer._buffer.get_system_timestamp() / 1e9
        finally:
            buffer.release()
        gaps.append(arrived - stamped)
    print(
        f"exposure {exposure * 1000:.1f} ms; arrival minus device stamp: "
        f"{', '.join(f'{g * 1000:.1f}' for g in gaps)} ms"
    )
    assert all(-0.01 < g < 1.0 for g in gaps)


def test_frame_matches_its_pixel_format(camera):
    frame = camera.capture_frame()
    print(f"{frame.pixel_format} {frame.pixels.shape} {frame.pixels.dtype}")
    assert frame.pixel_format == camera.setting(PIXEL_FORMAT).value
    assert frame.pixels.ndim in (2, 3)


def test_frame_honors_not_before_and_is_recent(camera):
    ready = datetime.now(UTC)
    frame = camera.capture_frame(not_before=ready)
    age = (frame.timestamp - ready).total_seconds()
    print(f"exposure start {age * 1000:.1f} ms, metadata {dict(frame.metadata)}")
    assert 0 <= age < MAX_FRAME_AGE_S + camera.setting(EXPOSURE_TIME).value


def test_the_newest_frame_is_taken_after_a_pause(camera):
    # Frames pile up while nobody grabs; the driver hands back the newest.
    time.sleep(1.0)
    called = datetime.now(UTC)
    frame = camera.capture_frame()
    age = (called - frame.timestamp).total_seconds()
    print(f"frame exposed {age * 1000:.1f} ms before the call")
    assert age < MAX_FRAME_AGE_S


def test_future_not_before_past_the_timeout_raises(camera):
    with pytest.raises(DeviceTimeout):
        camera.capture_frame(
            not_before=datetime.now(UTC) + timedelta(seconds=10), timeout=0.5
        )


def test_many_captures_do_not_exhaust_buffers(camera):
    stale = incomplete = remeasured = 0
    for _ in range(300):
        frame = camera.capture_frame(not_before=datetime.now(UTC), timeout=5.0)
        stale += frame.metadata["stale_frames"]
        incomplete += frame.metadata["incomplete_frames"]
        remeasured += frame.metadata["timestamp_remeasured"]
    print(
        f"300 captures: {stale} stale, {incomplete} incomplete, {remeasured} remeasured"
    )
    assert remeasured <= 300 // RETRIES_BEFORE_REMEASURE // 10


def test_capture_rate(camera):
    started = time.monotonic()
    for _ in range(30):
        camera.capture_frame(not_before=datetime.now(UTC), timeout=5.0)
    rate = 30 / (time.monotonic() - started)
    resulting = camera.settings().get("resulting_frame_rate")
    print(
        f"{rate:.2f} captures/s with not_before; camera reports "
        f"{resulting.value if resulting else 'no'} frames/s"
    )


def test_exposure_time_round_trips(restore):
    camera = restore
    if "ExposureAuto" in camera.settings():
        camera.set_setting("ExposureAuto", "Off")
    exposure = camera.setting(EXPOSURE_TIME)
    target = exposure.nearest(0.01)
    camera.set_setting(EXPOSURE_TIME, target)
    assert camera.setting(EXPOSURE_TIME).value == pytest.approx(target, rel=0.01)
    frame = camera.capture_frame(not_before=datetime.now(UTC))
    assert frame.metadata[EXPOSURE_TIME] == pytest.approx(target, rel=0.01)


def test_auto_exposure_locks_exposure_time(restore):
    # Checks that Aravis's access mode reflects the lock (usb3-vision.md).
    camera = restore
    if "ExposureAuto" not in camera.settings():
        pytest.skip("this camera has no ExposureAuto")
    camera.set_setting("ExposureAuto", "Continuous")
    assert camera.setting(EXPOSURE_TIME).read_only
    camera.set_setting("ExposureAuto", "Off")
    assert not camera.setting(EXPOSURE_TIME).read_only


def test_out_of_range_writes_are_refused_by_aravis(camera):
    # The range check the driver turns on, not the camera, should refuse this.
    conn = camera._connection
    low, _ = conn.float_range("ExposureTime")
    with pytest.raises(DeviceError):
        conn.set_float("ExposureTime", low / 2)


def test_every_pixel_format_captures(restore):
    # Changing the format pauses acquisition and may need new buffers.
    camera = restore
    for fmt in camera.setting(PIXEL_FORMAT).choices or ():
        camera.set_setting(PIXEL_FORMAT, fmt)
        frame = camera.capture_frame(not_before=datetime.now(UTC))
        print(f"{fmt}: {frame.pixels.shape} {frame.pixels.dtype}")
        assert frame.pixel_format == fmt
        if "16" in str(fmt):
            assert frame.pixels.dtype == np.uint16
        else:
            assert frame.pixels.dtype == np.uint8


def test_throughput_limit_round_trips_and_is_restored(camera):
    settings = camera.settings()
    if THROUGHPUT_LIMIT not in settings:
        pytest.skip("this camera has no DeviceLinkThroughputLimit")
    before = settings[THROUGHPUT_LIMIT]
    print(
        f"throughput limit {before.value} MB/s, range {before.minimum}-"
        f"{before.maximum}, step {before.step}"
    )
    target = before.nearest(80.0)
    try:
        camera.set_setting(THROUGHPUT_LIMIT, target)
        assert camera.setting(THROUGHPUT_LIMIT).value == pytest.approx(target)
        camera.capture_frame(not_before=datetime.now(UTC), timeout=5.0)
    finally:
        if before.value is not None:
            camera.set_setting(THROUGHPUT_LIMIT, before.value)


def test_software_trigger_captures_a_fresh_frame(restore):
    camera = restore
    if TRIGGER not in camera.settings():
        pytest.skip("this camera has no software trigger")
    started = time.monotonic()
    camera.set_setting(TRIGGER, "Software")
    print(
        f"switching took {time.monotonic() - started:.3f} s; trigger turnaround "
        f"{camera._trigger_turnaround:.3f} s"
    )
    try:
        for _ in range(20):
            ready = datetime.now(UTC)
            frame = camera.capture_frame(not_before=ready, timeout=5.0)
            start = (frame.timestamp - ready).total_seconds()
            print(
                f"exposure start {start * 1000:.1f} ms after the request, "
                f"{frame.metadata['retriggers']} triggers fired again"
            )
            assert 0 <= start < MAX_FRAME_AGE_S
    finally:
        camera.set_setting(TRIGGER, "Off")


def test_preview_is_small_and_eight_bit(camera):
    preview = camera.preview_frame()
    assert preview.dtype == np.uint8
    assert max(preview.shape[:2]) <= 1280


def test_a_second_open_of_the_same_camera_is_busy(camera, serial):
    # Settles whether "listed but won't open" means held (usb3-vision.md).
    with pytest.raises(DeviceBusy) as info:
        AravisCamera(serial).open()
    print(f"second open: {info.value}")


def test_reopening_lets_go_each_time(serial, camera):
    camera.close()
    try:
        for _ in range(3):
            with AravisCamera(serial) as again:
                again.capture_frame()
    finally:
        camera.open()


def test_every_camera_captures_together(camera):
    camera.close()  # the module's camera would otherwise be opened twice
    try:
        cameras = [AravisCamera(s) for s in attached()]
        errors: list[str] = []
        for c in cameras:
            c.open()
        try:
            for _ in range(20):
                ready = datetime.now(UTC)

                def capture(c: AravisCamera, ready: datetime = ready) -> None:
                    try:
                        frame = c.capture_frame(not_before=ready, timeout=5.0)
                        assert frame.timestamp >= ready
                    except (DeviceError, AssertionError) as exc:
                        errors.append(f"{c!r}: {exc}")

                threads = [threading.Thread(target=capture, args=(c,)) for c in cameras]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
        finally:
            for c in cameras:
                c.close()
        assert errors == []
    finally:
        camera.open()


def test_python_version(camera):
    print(f"Python {sys.version.split()[0]} on {sys.platform}")
