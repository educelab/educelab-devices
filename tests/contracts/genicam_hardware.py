"""Hardware checks every GenICam driver runs against a real camera.

A driver's hardware test module subclasses `GenICamHardwareTests`, marks it
``hardware``, and sets `driver` and `serial_variable` (and overrides `make` if
the driver needs more than a serial number). The camera is the one named by
that environment variable, or the first one attached; the multi-camera test
uses every attached camera. Settings changed by a test are put back
afterwards. Several tests print what they measured, for the hardware doc: run
with ``-s``.
"""

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import numpy as np
import pytest

from educelab.devices.cameras import EXPOSURE_TIME, PIXEL_FORMAT
from educelab.devices.cameras._genicam import (
    RETRIES_BEFORE_REMEASURE,
    THROUGHPUT_LIMIT,
    TRIGGER,
    GenICamCamera,
)
from educelab.devices.errors import DeviceBusy, DeviceError, DeviceTimeout

# Loose on purpose: these catch a broken offset or a wedged stream, not jitter.
OFFSET_SPREAD_S = 0.005
MAX_FRAME_AGE_S = 1.0


class GenICamHardwareTests:
    """Checks for one GenICam driver on real cameras."""

    driver: ClassVar[type[GenICamCamera]]
    serial_variable: ClassVar[str]

    @classmethod
    def make(cls, serial: str) -> GenICamCamera:
        return cls.driver(serial)  # type: ignore[call-arg]

    @classmethod
    def attached(cls) -> list[str]:
        try:
            infos = cls.driver.discover()
        except ImportError as exc:
            pytest.skip(str(exc))
        serials = [info.serial_number for info in infos if info.serial_number]
        if not serials:
            pytest.skip(f"no {cls.driver.label} cameras attached")
        return serials

    @pytest.fixture(scope="class")
    @classmethod
    def serial(cls) -> str:
        return os.environ.get(cls.serial_variable) or cls.attached()[0]

    @pytest.fixture(scope="class")
    @classmethod
    def camera(cls, serial):
        camera = cls.make(serial)
        try:
            camera.open()
        except ImportError as exc:
            pytest.skip(str(exc))
        yield camera
        camera.close()

    @pytest.fixture
    def restore(self, camera):
        """Put every writable setting back after the test."""
        saved = {n: s.value for n, s in camera.settings().items() if not s.read_only}
        yield camera
        # Auto modes first, so the values they lock become writable again.
        for name in sorted(saved, key=lambda n: not n.endswith("Auto")):
            value = saved[name]
            if value is not None and not camera.setting(name).read_only:
                camera.set_setting(name, value)

    def test_discover_reports_serial_model_and_address(self, serial):
        infos = {i.serial_number: i for i in self.driver.discover()}
        assert serial in infos
        print(f"discovered: {infos[serial]}")
        assert infos[serial].model

    def test_info_has_model_and_firmware(self, camera, serial):
        info = camera.info
        print(f"info: {info}")
        assert info.serial_number == serial
        assert info.model != f"{self.driver.label} camera"

    def test_settings_the_camera_offers(self, camera):
        # Records which optional features this model has (usb3-vision.md).
        for name, setting in camera.settings().items():
            print(f"{name}: {setting}")
        assert EXPOSURE_TIME in camera.settings()

    def test_camera_latches_a_timestamp(self, camera):
        print(f"timestamp node: {camera.timestamp_node}")
        assert camera.timestamp_node in ("Timestamp", "TimestampLatchValue")
        assert camera.timestamp_offset is not None

    def test_timestamp_offset_is_stable(self, camera):
        offsets = [camera.measure_timestamp_offset() for _ in range(5)]
        assert None not in offsets
        values = [o for o in offsets if o is not None]
        spread = max(values) - min(values)
        print(f"offset spread over 5 measurements: {spread * 1e6:.0f} us")
        assert spread < OFFSET_SPREAD_S

    def test_buffer_timestamps_are_the_device_counter(self, camera):
        # Aravis issue #887: some cameras put 0 in the buffer timestamp.
        frame = camera.capture_frame()
        print(f"device timestamp {frame.metadata['device_timestamp_ns']} ns")
        assert frame.metadata["device_timestamp_ns"] > 0

    def test_frame_matches_its_pixel_format(self, camera):
        frame = camera.capture_frame()
        print(f"{frame.pixel_format} {frame.pixels.shape} {frame.pixels.dtype}")
        assert frame.pixel_format == camera.setting(PIXEL_FORMAT).value
        assert frame.pixels.ndim in (2, 3)

    def test_frame_honors_not_before_and_is_recent(self, camera):
        ready = datetime.now(UTC)
        frame = camera.capture_frame(not_before=ready)
        age = (frame.timestamp - ready).total_seconds()
        print(f"exposure start {age * 1000:.1f} ms, metadata {dict(frame.metadata)}")
        assert 0 <= age < MAX_FRAME_AGE_S + camera.setting(EXPOSURE_TIME).value

    def test_the_newest_frame_is_taken_after_a_pause(self, camera):
        # Frames pile up while nobody grabs; the driver hands back the newest.
        time.sleep(1.0)
        called = datetime.now(UTC)
        frame = camera.capture_frame()
        age = (called - frame.timestamp).total_seconds()
        print(f"frame exposed {age * 1000:.1f} ms before the call")
        assert age < MAX_FRAME_AGE_S

    def test_future_not_before_past_the_timeout_raises(self, camera):
        with pytest.raises(DeviceTimeout):
            camera.capture_frame(
                not_before=datetime.now(UTC) + timedelta(seconds=10), timeout=0.5
            )

    def test_many_captures_do_not_exhaust_buffers(self, camera):
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

    def test_capture_rate(self, camera):
        started = time.monotonic()
        for _ in range(30):
            camera.capture_frame(not_before=datetime.now(UTC), timeout=5.0)
        rate = 30 / (time.monotonic() - started)
        resulting = camera.settings().get("resulting_frame_rate")
        print(
            f"{rate:.2f} captures/s with not_before; camera reports "
            f"{resulting.value if resulting else 'no'} frames/s"
        )

    def test_exposure_time_round_trips(self, restore):
        camera = restore
        if "ExposureAuto" in camera.settings():
            camera.set_setting("ExposureAuto", "Off")
        exposure = camera.setting(EXPOSURE_TIME)
        target = exposure.nearest(0.01)
        camera.set_setting(EXPOSURE_TIME, target)
        assert camera.setting(EXPOSURE_TIME).value == pytest.approx(target, rel=0.01)
        frame = camera.capture_frame(not_before=datetime.now(UTC))
        assert frame.metadata[EXPOSURE_TIME] == pytest.approx(target, rel=0.01)

    def test_auto_exposure_locks_exposure_time(self, restore):
        # Checks that the library's access mode reflects the lock (usb3-vision.md).
        camera = restore
        if "ExposureAuto" not in camera.settings():
            pytest.skip("this camera has no ExposureAuto")
        camera.set_setting("ExposureAuto", "Continuous")
        assert camera.setting(EXPOSURE_TIME).read_only
        camera.set_setting("ExposureAuto", "Off")
        assert not camera.setting(EXPOSURE_TIME).read_only

    def test_out_of_range_writes_are_refused(self, camera):
        # The library or the camera must refuse it; the driver's own check is
        # skipped by writing through the connection.
        node = camera._exposure_node
        if node is None:
            pytest.skip("this camera has no exposure time")
        conn = camera._connection
        low, _ = conn.float_range(node)
        with pytest.raises(DeviceError):
            conn.set_float(node, low / 2)

    def test_every_pixel_format_captures(self, restore):
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

    def test_throughput_limit_round_trips_and_is_restored(self, camera):
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

    def test_software_trigger_captures_a_fresh_frame(self, restore):
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

    def test_preview_is_small_and_eight_bit(self, camera):
        preview = camera.preview_frame()
        assert preview.dtype == np.uint8
        assert max(preview.shape[:2]) <= 1280

    def test_a_second_open_of_the_same_camera_is_busy(self, camera, serial):
        with pytest.raises(DeviceBusy) as info:
            self.make(serial).open()
        print(f"second open: {info.value}")

    def test_reopening_lets_go_each_time(self, serial, camera):
        camera.close()
        try:
            for _ in range(3):
                with self.make(serial) as again:
                    again.capture_frame()
        finally:
            camera.open()

    def test_every_camera_captures_together(self, camera):
        camera.close()  # the module's camera would otherwise be opened twice
        try:
            cameras = [self.make(s) for s in self.attached()]
            errors: list[str] = []
            for c in cameras:
                c.open()
            try:
                for _ in range(20):
                    ready = datetime.now(UTC)

                    def capture(c: GenICamCamera, ready: datetime = ready) -> None:
                        try:
                            frame = c.capture_frame(not_before=ready, timeout=5.0)
                            assert frame.timestamp >= ready
                        except (DeviceError, AssertionError) as exc:
                            errors.append(f"{c!r}: {exc}")

                    threads = [
                        threading.Thread(target=capture, args=(c,)) for c in cameras
                    ]
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
