"""gphoto2 tests against a real camera: ``pytest -m hardware -s tests/test_gphoto2_hardware.py``.

Uses the only camera attached, or ``GPHOTO2_PORT`` to pick one. Set the camera to
M with a lens attached and manual focus allowed; the focus tests move the lens to
both ends of its range. Run with ``-s`` to see the measurements printed for the
hardware doc.
"""

import os
import time

import numpy as np
import pytest

from educelab.devices.cameras import APERTURE, EXPOSURE_TIME
from educelab.devices.cameras.gphoto2 import GPhoto2Camera
from educelab.devices.errors import DeviceNotFound, FocusLimitReached

pytestmark = pytest.mark.hardware

NEF_MAGIC = (b"II*\x00", b"MM\x00*")
JPEG_MAGIC = b"\xff\xd8"


@pytest.fixture(scope="module")
def camera():
    camera = GPhoto2Camera(os.environ.get("GPHOTO2_PORT"))
    try:
        camera.open()
    except (DeviceNotFound, ImportError) as exc:
        pytest.skip(f"no gphoto2 camera: {exc}")
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
    print(f"\nmodel {info.model!r} at {info.address!r}")
    assert info.model != "unknown"


def test_reports_its_settings(camera):
    started = time.monotonic()
    settings = camera.settings()
    elapsed = time.monotonic() - started
    print(f"\nconfig read took {elapsed:.3f} s for {len(settings)} settings")
    for name in (EXPOSURE_TIME, "iso", APERTURE, "imagequality", "capturetarget"):
        if name in settings:
            print(f"{name}: {settings[name].value!r} of {settings[name].choices}")
    assert EXPOSURE_TIME in settings


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


def test_preview_is_an_rgb_frame(camera):
    started = time.monotonic()
    preview = camera.preview_frame()
    elapsed = time.monotonic() - started
    print(f"\npreview {preview.shape} in {elapsed:.3f} s")
    assert preview.dtype == np.uint8 and preview.ndim == 3


def test_aperture_choices_with_and_without_live_view(camera):
    if APERTURE not in camera.settings():
        pytest.skip("no aperture setting")
    camera.close()
    camera.open()
    before = camera.setting(APERTURE).choices
    camera.preview_frame()
    during = camera.setting(APERTURE).choices
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
