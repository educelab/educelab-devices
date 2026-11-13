"""gphoto2 tests against a real camera: ``pytest -m hardware -s tests/test_gphoto2_hardware.py``.

Uses the only camera attached, or ``GPHOTO2_PORT`` to pick one. Set the camera to
M and AF-S, with a lens that has a focus motor; the focus tests move the lens to
both ends of its range. Without a focus motor, the three focus tests fail with
``-1``. Run with ``-s`` to see the measurements printed for the
hardware doc.
"""

import os
import subprocess
import sys
import time
from datetime import UTC, datetime

import numpy as np
import pytest

from educelab.devices.cameras import APERTURE, EXPOSURE_TIME
from educelab.devices.cameras.gphoto2 import GPhoto2Camera
from educelab.devices.errors import DeviceBusy, DeviceNotFound, FocusLimitReached

pytestmark = pytest.mark.hardware

NEF_MAGIC = (b"II*\x00", b"MM\x00*")
JPEG_MAGIC = b"\xff\xd8"


@pytest.fixture(scope="module")
def camera():
    camera = GPhoto2Camera(os.environ.get("GPHOTO2_PORT"))
    started = time.monotonic()
    try:
        camera.open()
    except (DeviceNotFound, ImportError) as exc:
        pytest.skip(f"no gphoto2 camera: {exc}")
    # Includes the serial number and firmware reads, for the hardware doc.
    print(f"\nopened in {time.monotonic() - started:.3f} s")
    yield camera
    camera.close()


@pytest.fixture
def quality(camera):
    """Set ``imagequality`` for one test, restoring it afterwards."""
    original = camera.setting("imagequality").value

    def use(match):
        choices = camera.setting("imagequality").choices or ()
        chosen = next((c for c in choices if match(str(c))), None)
        if chosen is None:
            pytest.skip(f"no matching imagequality among {choices}")
        camera.set_setting("imagequality", chosen)
        return chosen

    yield use
    if original is not None:
        camera.set_setting("imagequality", original)


@pytest.fixture
def quick_exposure(camera):
    original = camera.setting(EXPOSURE_TIME).value
    exposure = camera.setting(EXPOSURE_TIME)
    camera.set_setting(EXPOSURE_TIME, exposure.nearest(1 / 60))
    yield
    # At Bulb there's no number to go back to; the camera stays at the new speed.
    if original is not None:
        camera.set_setting(EXPOSURE_TIME, original)


def test_identifies_the_camera(camera):
    info = camera.info
    print(
        f"\n{info.vendor!r} model {info.model!r} at {info.address!r}, serial "
        f"{info.serial_number!r}, firmware {info.firmware_version!r}"
    )
    assert info.model != "unknown"
    assert info.vendor


def test_reports_its_settings(camera):
    started = time.monotonic()
    settings = camera.settings()
    elapsed = time.monotonic() - started
    print(f"\nconfig read took {elapsed:.3f} s for {len(settings)} settings")
    for name in (EXPOSURE_TIME, "iso", APERTURE, "imagequality", "capturetarget"):
        if name in settings:
            print(f"{name}: {settings[name].value!r} of {settings[name].choices}")
    assert EXPOSURE_TIME in settings


def test_one_setting_is_read_without_the_whole_tree(camera):
    started = time.monotonic()
    camera.settings()
    full = time.monotonic() - started
    started = time.monotonic()
    camera.setting(EXPOSURE_TIME)
    single = time.monotonic() - started
    print(f"\nfull read {full:.3f} s, setting(exposure_time) {single:.3f} s")
    assert single < full / 2


def test_exposure_time_reads_back_as_set(camera):
    exposure = camera.setting(EXPOSURE_TIME)
    original = exposure.value
    try:
        for wanted in (1 / 250, 1 / 30, 0.5):
            value = exposure.nearest(wanted)
            camera.set_setting(EXPOSURE_TIME, value)
            assert camera.setting(EXPOSURE_TIME).value == value
    finally:
        if original is not None:
            camera.set_setting(EXPOSURE_TIME, original)


def test_bulb_and_auto_iso_read_as_none_until_a_number_is_written(camera):
    """Set the shutter to Bulb and ISO to Auto on the camera before running."""
    settings = camera.settings()
    at_bulb = [n for n in (EXPOSURE_TIME, "iso") if settings[n].value is None]
    if not at_bulb:
        pytest.skip("set the shutter to Bulb or ISO to Auto on the camera first")
    for name in at_bulb:
        setting = settings[name]
        print(f"\n{name} at a non-numeric value; {len(setting.choices or ())} choices")
        assert setting.choices
        camera.set_setting(name, setting.choices[0])
        assert camera.setting(name).value == setting.choices[0]


def test_raw_capture_returns_one_nef(camera, quality, quick_exposure):
    quality(lambda c: "NEF" in c and "+" not in c)
    files = camera.capture_files()
    print(f"\n{[(f.name, len(f.data)) for f in files]}")
    assert [f.suffix.upper() for f in files] == [".NEF"]
    assert files[0].data[:4] in NEF_MAGIC


def test_raw_and_jpeg_capture_returns_both(camera, quality, quick_exposure):
    quality(lambda c: "NEF" in c and "+" in c)
    files = camera.capture_files()
    print(f"\n{[(f.name, len(f.data)) for f in files]}")
    by_suffix = {f.suffix.upper(): f for f in files}
    assert set(by_suffix) == {".NEF", ".JPG"}
    assert by_suffix[".NEF"].data[:4] in NEF_MAGIC
    assert by_suffix[".JPG"].data[:2] == JPEG_MAGIC


def test_repeated_captures_stay_in_pc_mode(camera, quality, quick_exposure):
    quality(lambda c: "NEF" in c and "+" not in c)
    names = [camera.capture_files()[0].name for _ in range(10)]
    assert len(set(names)) == 10


def test_capture_after_an_idle_gap(camera, quality, quick_exposure):
    quality(lambda c: "NEF" in c and "+" not in c)
    time.sleep(10)
    assert camera.capture_files()


@pytest.fixture
def preview(camera):
    """The camera, with preview stopped again after the test."""
    yield camera
    camera.stop_preview()


def reopen(camera):
    """Open a camera just closed. macOS's ptpcamerad takes a released camera
    within moments, so it's stopped and the open tried again."""
    for _ in range(5):
        try:
            camera.open()
            return
        except DeviceBusy:
            if sys.platform != "darwin":
                raise
            subprocess.run(["killall", "-9", "ptpcamerad"], check=False)
            time.sleep(0.5)
    camera.open()


def similarity(a, b) -> float:
    """How alike two images are, from -1 to 1, whatever their levels."""
    a = np.asarray(a, dtype=np.float64) - np.mean(a)
    b = np.asarray(b, dtype=np.float64) - np.mean(b)
    return float((a * b).sum() / max(np.sqrt((a * a).sum() * (b * b).sum()), 1e-9))


def gray(pixels, size=None):
    from PIL import Image, ImageFilter

    image = Image.fromarray(pixels).convert("L")
    if size is not None:
        image = image.resize(size, Image.Resampling.BOX)
    # Blurred, so fine texture that aliases differently at each zoom (a mesh,
    # print) doesn't swamp the shapes.
    return np.asarray(image.filter(ImageFilter.BoxBlur(2)), dtype=np.float64)


def test_preview_is_an_rgb_frame(preview):
    geometry = preview.start_preview()
    started = time.monotonic()
    frame = preview.preview_frame()
    elapsed = time.monotonic() - started
    print(f"\npreview {geometry} in {elapsed:.3f} s")
    assert frame.pixel_format == "RGB8"
    assert frame.pixels.dtype == np.uint8 and frame.pixels.ndim == 3
    assert geometry.region.is_full


@pytest.mark.parametrize("side", [0.2, 0.1, 0.05])
@pytest.mark.parametrize("corner", [(0.65, 0.65), (0.15, 0.15), (0.8, 0.0)])
def test_a_zoomed_preview_shows_the_region_it_reports(preview, corner, side):
    """Point the camera at a still scene with distinct, unrepeated shapes all
    over the frame. Regions it can't find in the unzoomed frame are skipped."""
    preview.start_preview()
    whole = gray(preview.preview_frame().pixels)
    wanted = (*corner, side, side)
    started = time.monotonic()
    geometry = preview.start_preview(wanted)
    elapsed = time.monotonic() - started
    frame = preview.preview_frame()
    region = geometry.region
    zoom = preview.setting("liveviewzoomarea").value
    rows, cols = whole.shape
    width = max(round(region.width * cols), 8)
    height = max(round(region.height * rows), 8)
    seen = gray(frame.pixels, (width, height))
    if seen.std() < 3:
        pytest.skip(f"the target is blank at {wanted}")
    # Where the frame best matches the unzoomed one, near where it's reported.
    reach = round(0.08 * cols)
    x0, y0 = round(region.x * cols), round(region.y * rows)
    scores = {}
    for dy in range(-reach, reach + 1):
        for dx in range(-reach, reach + 1):
            x, y = x0 + dx, y0 + dy
            if x < 0 or y < 0 or x + width > cols or y + height > rows:
                continue
            scores[dx, dy] = similarity(seen, whole[y : y + height, x : x + width])
    at = max(scores, key=scores.__getitem__)
    best = scores[at]
    # A plain wall matches nearly as well somewhere else; that says nothing.
    far = 0.02 * cols
    elsewhere = max(
        (
            v
            for (dx, dy), v in scores.items()
            if abs(dx - at[0]) + abs(dy - at[1]) > far
        ),
        default=-1.0,
    )
    off = max(abs(at[0]) / cols, abs(at[1]) / rows)
    print(
        f"\n{wanted}: zoom {zoom}, {elapsed:.2f} s, {geometry.width}x"
        f"{geometry.height}; best match {best:.2f}, {at[0]:+d},{at[1]:+d} px "
        f"({off:.4f} of the frame) from the reported region"
    )
    if best < 0.8 or elsewhere > best - 0.05:
        pytest.skip(f"nothing distinct at {wanted} to find in the unzoomed frame")
    # Within 1% of the frame of where the driver says it is.
    assert off <= 0.01


def test_stopping_preview_restores_the_live_view_size_and_zoom(preview):
    before = {n: preview.setting(n).value for n in ("liveviewsize", "liveviewzoomarea")}
    preview.start_preview((0.45, 0.45, 0.1, 0.1))
    during = {n: preview.setting(n).value for n in before}
    preview.stop_preview()
    after = {n: preview.setting(n).value for n in before}
    print(f"\nbefore {before}, during {during}, after {after}")
    assert during != before and after == before


def test_capture_works_after_a_zoomed_preview(preview, quality, quick_exposure):
    quality(lambda c: "NEF" in c and "+" not in c)
    preview.start_preview((0.45, 0.45, 0.1, 0.1))
    preview.preview_frame()
    preview.stop_preview()
    assert preview.capture_files()


def test_preview_after_a_focus_step_is_from_after_it(preview):
    focus = preview.focus
    if focus is None:
        pytest.skip("camera has no focus drive")
    preview.start_preview((0.45, 0.45, 0.1, 0.1))
    for steps in (500, -500):
        try:
            focus.step(steps)
        except FocusLimitReached:
            # Already at that end, from an earlier test: go the other way.
            focus.step(-steps)
        ready = datetime.now(UTC)
        frame = preview.preview_frame(not_before=ready)
        assert frame.timestamp >= ready


def test_aperture_choices_with_and_without_live_view(preview):
    if APERTURE not in preview.settings():
        pytest.skip("no aperture setting")
    preview.close()
    reopen(preview)
    before = preview.setting(APERTURE).choices
    preview.start_preview()
    during = preview.setting(APERTURE).choices
    print(f"\naperture choices without live view: {before}")
    print(f"aperture choices with live view: {during}")
    assert during


@pytest.fixture
def focus(camera):
    focus = camera.focus
    if focus is None:
        pytest.skip("camera has no focus drive")
    return focus


def test_focus_steps_between_limits(focus):
    started = time.monotonic()
    focus.drive_to_limit("near")
    print(f"\nnear limit reached in {time.monotonic() - started:.1f} s")
    focus.step(500)
    assert focus.focus_steps == 500
    focus.step(-200)
    assert focus.focus_steps == 300


def test_stepping_past_a_limit_raises(focus):
    focus.drive_to_limit("far")
    with pytest.raises(FocusLimitReached) as raised:
        focus.step(100)
    assert raised.value.limit == "far"
    assert (focus.last_limit, focus.focus_steps) == ("far", 0)


def test_focus_range_in_steps(focus):
    """Counts the steps from near to far, for the hardware doc."""
    focus.drive_to_limit("near")
    steps = 0
    try:
        while True:
            focus.step(1000)
            steps += 1000
    except FocusLimitReached:
        pass
    print(f"\nnear to far: about {steps} steps (to the nearest 1000)")
    assert steps > 0
