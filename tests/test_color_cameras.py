"""Color GenICam cameras, on every backend's fake CM3-U3-31S4C.

What the core does with a color camera (formats, the Bayer pattern, white
balance) is shared, so each test runs through the core's own fake backend and
through the Aravis, GenTL, and PySpin fakes, which each hand pixels over
differently.
"""

import numpy as np
import pytest

from contracts import FrameCameraContract
from educelab.devices.cameras import (
    PIXEL_FORMAT,
    GenICamCamera,
    aravis,
    gentl,
    spinnaker,
)
from educelab.devices.cameras.aravis import AravisCamera
from educelab.devices.cameras.gentl import GenTLCamera
from educelab.devices.cameras.spinnaker import SpinnakerCamera
from fakes.aravis import FakeRepository
from fakes.genicam import FakeBackend, FakeCamera, cm3_31s4c
from fakes.gentl import PRODUCER, FakeGenicam
from fakes.pyspin import FakePySpin

SERIAL = "19299005"
WIDTH, HEIGHT = 16, 12


# Each attaches ``sim`` to a backend's fake library and returns a factory for
# cameras on it. Loaders are patched too, since discover() is a classmethod.


def _fake(sim, monkeypatch):
    backend = FakeBackend([sim])
    return lambda: GenICamCamera(SERIAL, backend)


def _aravis(sim, monkeypatch):
    repository = FakeRepository([sim])
    monkeypatch.setattr(aravis, "load_aravis", lambda: repository)
    # A fake Aravis stream receives the frames still in flight within a few polls.
    monkeypatch.setattr(aravis, "_DRAIN_QUIET_S", 0.01)
    return lambda: AravisCamera(SERIAL, repository=repository)


def _gentl(sim, monkeypatch):
    genicam = FakeGenicam([sim])
    monkeypatch.setattr(gentl, "load_genicam", lambda: genicam)
    monkeypatch.setattr(gentl, "find_producers", lambda: [PRODUCER])
    return lambda: GenTLCamera(SERIAL, producer=PRODUCER, genicam=genicam)


def _spinnaker(sim, monkeypatch):
    pyspin = FakePySpin([sim])
    monkeypatch.setattr(spinnaker, "load_pyspin", lambda: pyspin)
    return lambda: SpinnakerCamera(SERIAL, pyspin=pyspin)


@pytest.fixture(
    params=[_fake, _aravis, _gentl, _spinnaker], ids=lambda f: f.__name__[1:]
)
def make(request, monkeypatch):
    """A function from a fake camera to a factory of driver cameras on it."""
    return lambda sim: request.param(sim, monkeypatch)


@pytest.fixture
def sim():
    return cm3_31s4c(SERIAL, width=WIDTH, height=HEIGHT)


@pytest.fixture
def camera(make, sim):
    with make(sim)() as camera:
        yield camera


# --- Pixel formats ------------------------------------------------------------

# Every format the CM3-U3-31S4C offers but YCbCr411: shape, dtype, bit depth,
# and the value a saturated pixel reads.
FORMATS = {
    "Mono8": ((HEIGHT, WIDTH), np.uint8, 8, 255),
    "Mono12Packed": ((HEIGHT, WIDTH), np.uint16, 12, 4095),
    "Mono12p": ((HEIGHT, WIDTH), np.uint16, 12, 4095),
    # 12 bits held at the top (spinnaker.md).
    "Mono16": ((HEIGHT, WIDTH), np.uint16, 16, 65504),
    "BayerRG8": ((HEIGHT, WIDTH), np.uint8, 8, 255),
    "BayerRG16": ((HEIGHT, WIDTH), np.uint16, 16, 65535),
    "YCbCr422_8_CbYCrY": ((HEIGHT, WIDTH, 2), np.uint8, 8, 255),
    "YCbCr8_CbYCr": ((HEIGHT, WIDTH, 3), np.uint8, 8, 255),
    "RGB8": ((HEIGHT, WIDTH, 3), np.uint8, 8, 255),
}


def test_open_keeps_the_bayer_format_the_camera_opened_in(camera, sim):
    assert camera.setting(PIXEL_FORMAT).value == "BayerRG8"
    assert sim.get("PixelFormat") == "BayerRG8"


def test_every_format_but_ycbcr411_is_offered(camera):
    assert set(camera.setting(PIXEL_FORMAT).choices) == set(FORMATS)


@pytest.mark.parametrize("fmt", list(FORMATS))
def test_each_format_captures_in_its_layout(camera, sim, fmt):
    shape, dtype, depth, full = FORMATS[fmt]
    camera.set_setting(PIXEL_FORMAT, fmt)
    frame = camera.capture_frame()
    assert frame.pixel_format == fmt
    assert frame.pixels.shape == shape
    assert frame.pixels.dtype == dtype
    assert frame.metadata["bit_depth"] == depth
    assert frame.metadata["full_scale"] == full
    # The fake fills each frame with its count, so unpacking shows in the values.
    assert np.unique(frame.pixels).size == 1
    assert 0 < frame.pixels.flat[0] < 200


# --- Bayer pattern ------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["BayerRG8", "Mono8", "RGB8", "YCbCr422_8_CbYCrY"])
def test_frames_name_the_sensors_mosaic_in_every_format(camera, fmt):
    # The 31S4C's PixelColorFilter reads BayerRG even in Mono8 (spinnaker.md).
    camera.set_setting(PIXEL_FORMAT, fmt)
    assert camera.capture_frame().metadata["color_filter"] == "BayerRG"


def _core(**options):
    return GenICamCamera(SERIAL, FakeBackend([FakeCamera(SERIAL, **options)]))


def test_without_the_node_the_mosaic_comes_from_a_bayer_format():
    with _core(formats=["Mono8", "BayerGB8", "BayerGB16"]) as camera:
        assert camera.capture_frame().metadata["color_filter"] == "BayerGB"


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"color_filter": "None", "formats": ["Mono8", "Mono16"]},
    ],
    ids=["mono", "says none"],
)
def test_a_mono_sensor_has_no_mosaic(options):
    with _core(**options) as camera:
        assert camera.capture_frame().metadata["color_filter"] is None


# --- White balance ------------------------------------------------------------

RED, BLUE = "balance_ratio_red", "balance_ratio_blue"


def test_white_balance_starts_on_auto_with_the_ratios_read_only(camera):
    auto = camera.setting("BalanceWhiteAuto")
    assert auto.value == "Continuous"
    # Once is left out: it holds the ratios and doesn't revert to Off.
    assert auto.choices == ("Off", "Continuous")
    for name, value in ((RED, 2.0), (BLUE, 1.5)):
        ratio = camera.setting(name)
        assert ratio.value == value
        assert ratio.read_only
        assert (ratio.minimum, ratio.maximum) == (0.25, 4.0)
        assert ratio.unit is None


def test_ratios_are_written_with_auto_off(camera, sim):
    camera.set_setting("BalanceWhiteAuto", "Off")
    camera.set_setting(RED, 1.25)
    camera.set_setting(BLUE, 3.0)
    assert camera.setting(RED).value == 1.25
    assert camera.setting(BLUE).value == 3.0
    assert sim.nodes["BalanceRatio"].value == {"Red": 1.25, "Blue": 3.0}


@pytest.mark.parametrize("start", ["Red", "Blue"])
def test_reading_and_writing_a_ratio_leaves_the_selector_as_found(camera, sim, start):
    sim.set("BalanceRatioSelector", start)
    camera.set_setting("BalanceWhiteAuto", "Off")
    camera.settings()
    camera.set_setting(RED, 1.0)
    camera.set_setting(BLUE, 1.0)
    camera.capture_frame()
    assert sim.get("BalanceRatioSelector") == start


def test_a_failed_ratio_read_still_restores_the_selector(camera, sim):
    from fakes.genicam import FakeCameraError

    sim.failures["read BalanceRatio"] = FakeCameraError("access", "refused")
    with pytest.raises(Exception, match="refused"):
        camera.setting(BLUE)
    assert sim.get("BalanceRatioSelector") == "Red"


def test_frames_record_the_white_balance(camera):
    camera.set_setting("BalanceWhiteAuto", "Off")
    camera.set_setting(BLUE, 0.5)
    metadata = camera.capture_frame().metadata
    assert metadata["BalanceWhiteAuto"] == "Off"
    assert metadata[RED] == 2.0
    assert metadata[BLUE] == 0.5


def test_fixed_ratios_are_read_once_for_frame_metadata():
    sim = cm3_31s4c(SERIAL, width=WIDTH, height=HEIGHT)
    backend = FakeBackend([sim])
    with GenICamCamera(SERIAL, backend) as camera:
        camera.set_setting("BalanceWhiteAuto", "Off")
        camera.capture_frame()
        reads = backend.connections[-1].node_reads
        reads.clear()
        assert camera.capture_frame().metadata[BLUE] == 1.5
        # Only the mode, to know the ratios can't have moved.
        assert "BalanceRatio" not in reads
        camera.set_setting(BLUE, 0.75)
        assert camera.capture_frame().metadata[BLUE] == 0.75
        # Under auto, the camera moves them between frames, so each is read.
        camera.set_setting("BalanceWhiteAuto", "Continuous")
        sim.nodes["BalanceRatio"].value["Blue"] = 1.25
        assert camera.capture_frame().metadata[BLUE] == 1.25
        sim.nodes["BalanceRatio"].value["Blue"] = 1.0
        assert camera.capture_frame().metadata[BLUE] == 1.0


def test_white_balance_comes_and_goes_with_the_pixel_format(camera):
    names = {"BalanceWhiteAuto", RED, BLUE}
    camera.set_setting(PIXEL_FORMAT, "Mono8")
    assert not names & set(camera.settings())
    assert not names & set(camera.capture_frame().metadata)
    camera.set_setting(PIXEL_FORMAT, "RGB8")
    assert names <= set(camera.settings())
    assert names <= set(camera.capture_frame().metadata)


def test_a_camera_without_white_balance_offers_none():
    with _core(color=True) as camera:
        assert not {"BalanceWhiteAuto", RED, BLUE} & set(camera.settings())
        assert RED not in camera.capture_frame().metadata


# --- Contracts ----------------------------------------------------------------


class TestColorCM3Contract(FrameCameraContract):
    """The frame camera rules, for the CM3-U3-31S4C on every backend."""

    @pytest.fixture
    def make_device(self, make):
        return make(cm3_31s4c(SERIAL, width=WIDTH, height=HEIGHT))
