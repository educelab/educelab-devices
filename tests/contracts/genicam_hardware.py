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
import statistics
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import numpy as np
import pytest

from educelab.devices.cameras import (
    EXPOSURE_TIME,
    HEIGHT,
    PIXEL_FORMAT,
    THROUGHPUT_LIMIT,
    TRIGGER,
    WIDTH,
    GenICamCamera,
    PreviewRegion,
)
from educelab.devices.cameras._genicam import RETRIES_BEFORE_REMEASURE
from educelab.devices.errors import DeviceBusy, DeviceError, DeviceTimeout

CENTER = PreviewRegion(0.25, 0.25, 0.5, 0.5)


def correlation(a, b) -> float:
    """How alike two images are, from -1 to 1, whatever their levels."""
    a = np.asarray(a, dtype=np.float64) - np.mean(a)
    b = np.asarray(b, dtype=np.float64) - np.mean(b)
    return float((a * b).sum() / max(np.sqrt((a * a).sum() * (b * b).sum()), 1e-9))


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

    @staticmethod
    def require_timing(camera: GenICamCamera) -> None:
        """Skip a test that needs frames timed against the host clock."""
        if camera.timestamp_source is None:
            pytest.skip(
                f"{camera!r} has no timestamp latch and its library reports no "
                "arrival times, so its frames can't be timed (usb3-vision.md)"
            )

    @staticmethod
    def fresh(camera: GenICamCamera) -> datetime | None:
        """A not_before for "a frame from now on", or None if the camera can't
        take one."""
        return datetime.now(UTC) if camera.timestamp_source else None

    def test_discover_reports_serial_model_and_address(self, serial):
        infos = {i.serial_number: i for i in self.driver.discover()}
        assert serial in infos
        print(f"discovered: {infos[serial]}")
        assert infos[serial].model

    def test_info_has_model_and_firmware(self, camera, serial):
        # The camera's own serial, which can differ from the one it was opened by
        # (Aravis lists FLIR cameras by USB serial).
        info = camera.info
        print(f"info: {info} (opened as {serial})")
        assert info.serial_number
        assert info.vendor
        assert info.model != f"{self.driver.label} camera"

    def test_opening_keeps_the_discovered_vendor_and_model(self, camera, serial):
        # Discovery reads the transport layer, an open camera its own nodes.
        # Consumers write these into EXIF and key saved config on them.
        opened = camera.info
        camera.close()  # a backend may not list a camera that is open
        try:
            listed = {i.serial_number: i for i in self.driver.discover()}
        finally:
            camera.open()
        assert serial in listed
        found = listed[serial]
        print(f"discovered: {found.vendor!r} {found.model!r}")
        print(f"opened: {opened.vendor!r} {opened.model!r}")
        assert (opened.vendor, opened.model) == (found.vendor, found.model)

    def test_time_to_read_and_write_one_setting(self, restore):
        # Prints the times the hardware docs record (Settings).
        camera = restore
        if "ExposureAuto" in camera.settings():
            camera.set_setting("ExposureAuto", "Off")
        exposure = camera.setting(EXPOSURE_TIME)
        low = max(exposure.minimum or 0.0, exposure.value * 0.9)
        values = (exposure.value, low)

        def median_ms(fn) -> float:
            times = []
            for i in range(20):
                started = time.perf_counter()
                fn(i)
                times.append(time.perf_counter() - started)
            return statistics.median(times) * 1000

        every = median_ms(lambda i: camera.settings())
        one = median_ms(lambda i: camera.setting(EXPOSURE_TIME))
        write = median_ms(lambda i: camera.set_setting(EXPOSURE_TIME, values[i % 2]))
        print(
            f"median of 20: settings() {every:.1f} ms, setting() {one:.1f} ms, "
            f"set_setting() {write:.1f} ms"
        )
        assert camera.setting(EXPOSURE_TIME).value == pytest.approx(low, rel=0.01)

    def test_settings_the_camera_offers(self, camera):
        # Records which optional features this model has (usb3-vision.md).
        for name, setting in camera.settings().items():
            print(f"{name}: {setting}")
        assert EXPOSURE_TIME in camera.settings()

    def test_frame_size_matches_a_frame(self, camera):
        size = (camera.setting(HEIGHT).value, camera.setting(WIDTH).value)
        assert camera.capture_frame().pixels.shape[:2] == size

    def test_camera_has_a_timestamp_offset(self, camera):
        # Records which node or source this model uses (usb3-vision.md).
        print(
            f"timestamp node: {camera.timestamp_node}, source {camera.timestamp_source}"
        )
        self.require_timing(camera)
        assert camera.timestamp_source in ("latch", "arrival")
        assert camera.timestamp_offset is not None

    def test_timestamp_offset_is_stable(self, camera):
        self.require_timing(camera)
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
        self.require_timing(camera)
        ready = datetime.now(UTC)
        frame = camera.capture_frame(not_before=ready)
        age = (frame.timestamp - ready).total_seconds()
        print(f"exposure start {age * 1000:.1f} ms, metadata {dict(frame.metadata)}")
        assert 0 <= age < MAX_FRAME_AGE_S + camera.setting(EXPOSURE_TIME).value

    def test_the_newest_frame_is_taken_after_a_pause(self, camera):
        # Frames pile up while nobody grabs; the driver hands back the newest.
        self.require_timing(camera)
        time.sleep(1.0)
        called = datetime.now(UTC)
        frame = camera.capture_frame()
        age = (called - frame.timestamp).total_seconds()
        print(f"frame exposed {age * 1000:.1f} ms before the call")
        assert age < MAX_FRAME_AGE_S

    def test_future_not_before_past_the_timeout_raises(self, camera):
        self.require_timing(camera)
        with pytest.raises(DeviceTimeout):
            camera.capture_frame(
                not_before=datetime.now(UTC) + timedelta(seconds=10), timeout=0.5
            )

    def test_many_captures_do_not_exhaust_buffers(self, camera):
        stale = incomplete = remeasured = 0
        for _ in range(300):
            frame = camera.capture_frame(not_before=self.fresh(camera), timeout=5.0)
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
            camera.capture_frame(not_before=self.fresh(camera), timeout=5.0)
        rate = 30 / (time.monotonic() - started)
        resulting = camera.settings().get("resulting_frame_rate")
        print(
            f"{rate:.2f} captures/s "
            f"{'with' if self.fresh(camera) else 'without'} not_before; camera reports "
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
        frame = camera.capture_frame(not_before=self.fresh(camera))
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

    def test_out_of_range_writes_are_refused_or_clamped(self, restore):
        # Written through the connection, past the driver's own range check, to
        # record what the library and camera do (usb3-vision.md).
        camera = restore
        if "ExposureAuto" in camera.settings():
            camera.set_setting("ExposureAuto", "Off")
        node = camera._exposure_node
        if node is None or not camera._connection.is_writable(node):
            pytest.skip("this camera has no writable exposure time")
        conn = camera._connection
        low, _ = conn.float_range(node)
        try:
            conn.set_float(node, low / 2)
        except DeviceError as exc:
            print(f"out-of-range write refused: {exc}")
            return
        print(f"out-of-range write of {low / 2} accepted; reads {conn.get_float(node)}")
        assert conn.get_float(node) >= low

    def test_every_pixel_format_captures(self, restore):
        # Changing the format pauses acquisition and may need new buffers.
        camera = restore
        for fmt in camera.setting(PIXEL_FORMAT).choices or ():
            camera.set_setting(PIXEL_FORMAT, fmt)
            frame = camera.capture_frame(not_before=self.fresh(camera))
            depth = frame.metadata["bit_depth"]
            print(
                f"{fmt}: {frame.pixels.shape} {frame.pixels.dtype}, {depth} bits, "
                f"max {frame.pixels.max()}"
            )
            assert frame.pixel_format == fmt
            assert frame.pixels.dtype == (np.uint8 if depth == 8 else np.uint16)
            assert int(frame.pixels.max()) < 2**depth

    def test_throughput_limit_round_trips_and_is_restored(self, restore):
        camera = restore
        settings = camera.settings()
        if THROUGHPUT_LIMIT not in settings:
            pytest.skip("this camera has no DeviceLinkThroughputLimit")
        # The shortest exposure, so the limit and not the exposure sets the rate.
        if "ExposureAuto" in settings:
            camera.set_setting("ExposureAuto", "Off")
        shortest = camera.setting(EXPOSURE_TIME).minimum
        if shortest is not None:
            camera.set_setting(EXPOSURE_TIME, shortest)
        before = settings[THROUGHPUT_LIMIT]
        print(
            f"throughput limit {before.value} MB/s, range {before.minimum}-"
            f"{before.maximum}, step {before.step}"
        )
        target = before.nearest(80.0)
        try:
            camera.set_setting(THROUGHPUT_LIMIT, target)
            assert camera.setting(THROUGHPUT_LIMIT).value == pytest.approx(target)
            frame = camera.capture_frame(not_before=self.fresh(camera), timeout=5.0)
            rate = camera.settings().get("resulting_frame_rate")
            if rate is not None and rate.value:
                # Bytes per second allows ~target / frame size frames/s; bits
                # would allow an eighth of that. This settles the limit's unit.
                expected = target * 1e6 / frame.pixels.nbytes
                print(
                    f"resulting {rate.value:.2f} frames/s; bytes would allow "
                    f"{expected:.2f}"
                )
                assert expected / 4 < rate.value <= expected * 1.1
        finally:
            if before.value is not None:
                camera.set_setting(THROUGHPUT_LIMIT, before.value)

    def test_software_trigger_captures_a_fresh_frame(self, restore):
        camera = restore
        if TRIGGER not in camera.settings():
            pytest.skip("this camera has no software trigger")
        self.require_timing(camera)
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

    def test_not_before_is_refused_when_frames_cant_be_timed(self, camera):
        if camera.timestamp_source is not None:
            pytest.skip(f"{camera!r} times its frames ({camera.timestamp_source})")
        with pytest.raises(DeviceError, match="not_before"):
            camera.capture_frame(not_before=datetime.now(UTC), timeout=5.0)

    @pytest.fixture
    def preview(self, camera):
        """The camera, with preview stopped again after the test."""
        yield camera
        camera.stop_preview()

    def test_a_whole_sensor_preview_is_the_capture_frame(self, preview):
        capture = preview.capture_frame()
        started = time.perf_counter()
        geometry = preview.start_preview()
        print(f"\nfull preview started in {time.perf_counter() - started:.3f} s")
        frame = preview.preview_frame()
        assert geometry.region.is_full
        assert frame.pixel_format == capture.pixel_format
        assert frame.pixels.shape == capture.pixels.shape
        assert frame.geometry == geometry

    def test_a_region_preview_shows_that_part_of_the_sensor(self, preview):
        whole = preview.capture_frame().pixels.astype(np.float64)
        started = time.perf_counter()
        geometry = preview.start_preview(CENTER)
        elapsed = time.perf_counter() - started
        frame = preview.preview_frame()
        region = geometry.region
        print(f"\nregion preview started in {elapsed:.3f} s: {geometry}")
        assert CENTER.contains(region)
        assert frame.pixels.shape[:2] == (geometry.height, geometry.width)
        rows, cols = whole.shape[:2]
        y, x = round(region.y * rows), round(region.x * cols)
        expected = whole[y : y + geometry.height, x : x + geometry.width]
        if expected.std() < 2:
            pytest.skip("the scene is too flat to tell where a region is")
        match = correlation(frame.pixels, expected)
        shifted = correlation(frame.pixels, np.roll(expected, 32, axis=1))
        print(f"matches the capture's centre {match:.3f}; shifted 32 px {shifted:.3f}")
        assert match > 0.8 and match > shifted

    def test_moving_a_region_keeps_its_size_and_shows_the_new_place(self, preview):
        first = preview.start_preview((0.2, 0.2, 0.25, 0.25))
        preview.preview_frame()
        started = time.perf_counter()
        moved = preview.start_preview((0.5, 0.45, 0.25, 0.25))
        elapsed = time.perf_counter() - started
        frame = preview.preview_frame()
        waited = time.perf_counter() - started
        print(f"\nmoved in {elapsed:.3f} s; first frame {waited:.3f} s after asking")
        assert frame.geometry == moved != first
        assert frame.pixels.shape[:2] == (moved.height, moved.width)
        preview.stop_preview()
        whole_after = preview.capture_frame().pixels.astype(np.float64)
        rows, cols = whole_after.shape[:2]
        y, x = round(moved.region.y * rows), round(moved.region.x * cols)
        expected = whole_after[y : y + moved.height, x : x + moved.width]
        if expected.std() < 2:
            pytest.skip("the scene is too flat to tell where a region is")
        # The first frame after the move already shows the new place.
        assert correlation(frame.pixels, expected) > 0.8

    def test_max_size_reduces_without_changing_the_level(self, preview):
        whole = preview.capture_frame().pixels
        started = time.perf_counter()
        geometry = preview.start_preview(max_size=600)
        elapsed = time.perf_counter() - started
        frame = preview.preview_frame()
        ratio = frame.pixels.mean() / max(whole.mean(), 1e-9)
        rate = preview.setting("resulting_frame_rate").value
        print(
            f"\nmax_size=600 in {elapsed:.3f} s: {geometry.width}x{geometry.height}, "
            f"level {ratio:.3f}x the capture's, {rate:.1f} frames/s"
        )
        assert max(geometry.width, geometry.height) <= 600
        assert frame.pixels.shape[:2] == (geometry.height, geometry.width)
        # Decimation keeps the level; binning would be about 4x.
        assert 0.8 < ratio < 1.25

    def test_stopping_preview_restores_captures_and_the_link(self, preview):
        before = preview.capture_frame()
        settings = preview.settings()
        preview.start_preview((0.4, 0.4, 0.2, 0.2), max_size=100)
        preview.preview_frame()
        started = time.perf_counter()
        preview.stop_preview()
        print(f"\nstopped in {time.perf_counter() - started:.3f} s")
        after = preview.capture_frame()
        assert after.pixels.shape == before.pixels.shape
        now = preview.settings()
        for name in (THROUGHPUT_LIMIT, "resulting_frame_rate", WIDTH, HEIGHT):
            if name in settings:
                print(f"{name}: {settings[name].value} before, {now[name].value} after")
        if settings.get(THROUGHPUT_LIMIT) and settings[THROUGHPUT_LIMIT].value:
            limit = settings[THROUGHPUT_LIMIT].value
            assert now[THROUGHPUT_LIMIT].value == pytest.approx(limit, rel=0.01)
        rate = settings["resulting_frame_rate"].value
        assert now["resulting_frame_rate"].value == pytest.approx(rate, rel=0.1)

    def test_preview_frames_honor_not_before(self, preview):
        self.require_timing(preview)
        preview.start_preview((0.4, 0.4, 0.2, 0.2))
        ready = datetime.now(UTC)
        frame = preview.preview_frame(not_before=ready)
        print(f"\nframe {(frame.timestamp - ready).total_seconds() * 1e3:.1f} ms on")
        assert frame.timestamp >= ready

    def test_eight_bit_frames_are_the_top_bits_of_deeper_ones(self, restore):
        formats = restore.setting(PIXEL_FORMAT).choices or ()
        deeper = [f for f in formats if f in ("Mono16", "Mono10g40IDS")]
        if "Mono8" not in formats or not deeper:
            pytest.skip(f"needs Mono8 and a deeper mono format, not {formats}")
        restore.set_setting(PIXEL_FORMAT, "Mono8")
        eight = restore.capture_frame(not_before=self.fresh(restore)).pixels
        for name in deeper:
            restore.set_setting(PIXEL_FORMAT, name)
            frame = restore.capture_frame(not_before=self.fresh(restore))
            deep = frame.pixels.astype(np.int64)
            top = int(deep.max()).bit_length()
            # Left-aligned 16-bit data fills the top; right-aligned stops short.
            depth = 16 if top > 12 else frame.metadata["bit_depth"]
            as_eight = deep >> (depth - 8)
            diff = np.abs(as_eight - eight.astype(np.int64))
            print(
                f"\n{name}: highest bit used {top}, read as {depth}-bit; Mono8 "
                f"differs by {diff.mean():.2f} counts on average, {diff.max()} at "
                f"most; clipped {np.mean(eight == 255):.4f} in Mono8, "
                f"{np.mean(as_eight >= 255):.4f} in {name}"
            )
            # A still, lit scene: only noise between the two frames.
            assert diff.mean() < 3

    def test_a_second_open_of_the_same_camera_is_busy(self, camera, serial):
        second = self.make(serial)
        try:
            second.open()
        except DeviceBusy as exc:
            print(f"second open: {exc}")
            return
        second.close()
        pytest.fail("a second open of the same camera succeeded")

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
            # Cameras of this model only: a rig of mixed cameras uncapped can
            # need more than the shared USB link has (usb3-vision.md).
            same = [
                i.serial_number
                for i in self.driver.discover()
                if i.serial_number and i.model == camera.info.model
            ]
            cameras = [self.make(s) for s in same]
            errors: list[str] = []
            for c in cameras:
                c.open()
            try:
                for _ in range(20):
                    ready = datetime.now(UTC)

                    def capture(c: GenICamCamera, ready: datetime = ready) -> None:
                        # Cameras that can't latch their timestamp can't take
                        # not_before; they just capture.
                        latched = c.timestamp_offset is not None
                        try:
                            frame = c.capture_frame(
                                not_before=ready if latched else None, timeout=5.0
                            )
                            assert not latched or frame.timestamp >= ready
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
