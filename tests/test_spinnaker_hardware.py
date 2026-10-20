"""Spinnaker tests against real cameras: ``pytest -m hardware tests/test_spinnaker_hardware.py``.

Needs PySpin (Python 3.12). Uses the camera named by ``SPINNAKER_SERIAL``,
or the first one attached; the multi-camera tests use every attached camera. Settings
changed by a test are put back afterwards.
"""

import os
import sys
import threading
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from educelab.devices.cameras import EXPOSURE_TIME, PIXEL_FORMAT
from educelab.devices.cameras.spinnaker import (
    RETRIES_BEFORE_REMEASURE,
    THROUGHPUT_LIMIT,
    TRIGGER,
    SpinnakerCamera,
    SpinnakerSystem,
)
from educelab.devices.errors import DeviceError, DeviceTimeout

pytestmark = pytest.mark.hardware

# Loose on purpose: these catch a broken offset or a wedged stream, not jitter.
OFFSET_SPREAD_S = 0.005
MAX_FRAME_AGE_S = 1.0


def attached() -> list[str]:
    try:
        infos = SpinnakerSystem().discover()
    except ImportError as exc:
        pytest.skip(str(exc))
    serials = [info.serial_number for info in infos if info.serial_number]
    if not serials:
        pytest.skip("no Spinnaker cameras attached")
    return serials


@pytest.fixture(scope="module")
def serial() -> str:
    return os.environ.get("SPINNAKER_SERIAL") or attached()[0]


@pytest.fixture(scope="module")
def camera(serial):
    camera = SpinnakerCamera(serial)
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


def test_discover_reports_serial_and_model(serial):
    infos = {i.serial_number: i for i in SpinnakerSystem().discover()}
    assert serial in infos
    assert infos[serial].model


def test_info_has_model_and_firmware(camera, serial):
    info = camera.info
    assert info.serial_number == serial
    assert info.model != "Spinnaker camera"
    assert info.firmware_version


def test_camera_latches_a_timestamp(camera):
    # Records which node this model uses (docs/hardware/spinnaker.md, Timestamps).
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


def test_frame_matches_its_pixel_format(camera):
    frame = camera.capture_frame()
    assert frame.pixel_format == camera.setting(PIXEL_FORMAT).value
    assert frame.pixels.ndim in (2, 3)


def test_frame_honors_not_before_and_is_recent(camera):
    ready = datetime.now(UTC)
    frame = camera.capture_frame(not_before=ready)
    age = (frame.timestamp - ready).total_seconds()
    print(f"exposure start {age * 1000:.1f} ms, metadata {dict(frame.metadata)}")
    assert 0 <= age < MAX_FRAME_AGE_S + camera.setting(EXPOSURE_TIME).value


def test_future_not_before_past_the_timeout_raises(camera):
    with pytest.raises(DeviceTimeout):
        camera.capture_frame(
            not_before=datetime.now(UTC) + timedelta(seconds=10), timeout=0.5
        )


def test_many_captures_do_not_exhaust_buffers(camera):
    # pgs-scan #83 leaked a buffer per stale frame until GetNextImage blocked.
    stale = incomplete = remeasured = 0
    dtype = camera.capture_frame().pixels.dtype
    refs = sys.getrefcount(dtype)
    for _ in range(300):
        frame = camera.capture_frame(not_before=datetime.now(UTC), timeout=5.0)
        stale += frame.metadata["stale_frames"]
        incomplete += frame.metadata["incomplete_frames"]
        remeasured += frame.metadata["timestamp_remeasured"]
    del frame
    drift = sys.getrefcount(dtype) - refs
    print(
        f"300 captures: {stale} stale, {incomplete} incomplete, {remeasured} "
        f"remeasured; {dtype} refcount drift {drift}"
    )
    # PySpin dropping a dtype reference per frame would show up as about -300.
    assert drift > -10
    # Re-measuring every few frames would mean the offset is wrong, not stale frames.
    assert remeasured <= 300 // RETRIES_BEFORE_REMEASURE // 10


def test_exposure_time_round_trips(restore):
    camera = restore
    camera.set_setting("ExposureAuto", "Off")
    exposure = camera.setting(EXPOSURE_TIME)
    target = exposure.nearest(0.01)
    camera.set_setting(EXPOSURE_TIME, target)
    assert camera.setting(EXPOSURE_TIME).value == pytest.approx(target, rel=0.01)
    frame = camera.capture_frame(not_before=datetime.now(UTC))
    assert frame.metadata[EXPOSURE_TIME] == pytest.approx(target, rel=0.01)


def test_auto_exposure_locks_exposure_time(restore):
    camera = restore
    camera.set_setting("ExposureAuto", "Continuous")
    assert camera.setting(EXPOSURE_TIME).read_only
    camera.set_setting("ExposureAuto", "Off")
    assert not camera.setting(EXPOSURE_TIME).read_only


def test_every_pixel_format_captures(restore):
    # Changing the format pauses and resumes acquisition.
    camera = restore
    for fmt in camera.setting(PIXEL_FORMAT).choices or ():
        camera.set_setting(PIXEL_FORMAT, fmt)
        frame = camera.capture_frame(not_before=datetime.now(UTC))
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
        frame = camera.capture_frame(not_before=datetime.now(UTC), timeout=5.0)
        rate = camera.settings().get("resulting_frame_rate")
        if rate is not None and rate.value:
            # Bytes per second gives ~target / frame size frames/s; bits would give
            # an eighth of that. This settles the node's unit.
            expected = target * 1e6 / frame.pixels.nbytes
            print(
                f"resulting {rate.value:.2f} frames/s; bytes would allow {expected:.2f}"
            )
            assert rate.value <= expected * 1.1
            assert rate.value > expected / 4
    finally:
        # A limit mode switched on by the write stays on; the value is restored.
        if before.value is not None:
            camera.set_setting(THROUGHPUT_LIMIT, before.value)
    assert camera.setting(THROUGHPUT_LIMIT).value == before.value


def test_software_trigger_captures_a_fresh_frame(restore):
    camera = restore
    if TRIGGER not in camera.settings():
        pytest.skip("this camera has no software trigger")
    camera.set_setting(TRIGGER, "Software")
    retriggers = 0
    try:
        for _ in range(20):
            ready = datetime.now(UTC)
            frame = camera.capture_frame(not_before=ready, timeout=5.0)
            start = (frame.timestamp - ready).total_seconds()
            assert 0 <= start < MAX_FRAME_AGE_S
            retriggers += frame.metadata["retriggers"]
        print(
            f"exposure start {start * 1000:.1f} ms after the request, "
            f"{retriggers} triggers fired again in 20 captures, {frame.metadata}"
        )
    finally:
        camera.set_setting(TRIGGER, "Off")


def test_preview_is_small_and_eight_bit(camera):
    preview = camera.preview_frame()
    assert preview.dtype == np.uint8
    assert max(preview.shape[:2]) <= 1280


def test_reopening_releases_the_sdk_each_time(serial, camera):
    # Needs the module's camera closed: holding it would keep the system open.
    camera.close()
    try:
        for _ in range(3):
            with SpinnakerCamera(serial) as again:
                again.capture_frame()
        with SpinnakerSystem() as system:
            assert system.discover()
    finally:
        camera.open()


def test_every_camera_captures_together(camera):
    camera.close()  # the module's camera would otherwise be opened twice
    try:
        cameras = [SpinnakerCamera(s) for s in attached()]
        errors: list[str] = []
        for c in cameras:
            c.open()
        try:
            for _ in range(20):
                ready = datetime.now(UTC)

                def capture(c: SpinnakerCamera, ready: datetime = ready) -> None:
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
