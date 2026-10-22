"""The fake Aravis behaves like the real one where the driver relies on it.

The checks against real Aravis skip unless Aravis 0.8 and PyGObject are
installed, as on a rig machine; CI has neither.
"""

import pytest

from educelab.devices.cameras import aravis
from fakes.aravis import (
    PFNC,
    FakeArvBuffer,
    FakeArvCamera,
    FakeArvDevice,
    FakeArvStream,
    FakeRepository,
)
from fakes.genicam import FakeCamera


@pytest.fixture
def repository():
    return FakeRepository([FakeCamera("1")])


@pytest.fixture(scope="module")
def real():
    try:
        return aravis.load_aravis().Aravis
    except ImportError as exc:
        pytest.skip(str(exc))


def test_dropping_the_camera_lets_go_of_it(repository):
    arv = repository.Aravis
    arv.update_device_list()
    camera = arv.Camera.new(arv.get_device_id(0))
    stream = camera.create_stream(None, None)
    assert repository.cameras[0].opened
    del camera
    assert repository.cameras[0].opened  # the stream still holds it
    del stream
    assert not repository.cameras[0].opened


def test_try_pop_only_takes_frames_that_have_arrived(repository):
    arv = repository.Aravis
    arv.update_device_list()
    camera = arv.Camera.new(arv.get_device_id(0))
    stream = camera.create_stream(None, None)
    stream.push_buffer(arv.Buffer.new_allocate(camera.get_payload()))
    camera.start_acquisition()
    assert stream.try_pop_buffer() is None
    assert stream.timeout_pop_buffer(1000) is not None


def test_a_frame_needs_a_free_buffer(repository):
    arv = repository.Aravis
    arv.update_device_list()
    camera = arv.Camera.new(arv.get_device_id(0))
    stream = camera.create_stream(None, None)
    camera.start_acquisition()
    assert stream.timeout_pop_buffer(1000) is None


def test_a_buffer_too_small_for_the_frame_is_a_size_mismatch(repository):
    arv = repository.Aravis
    arv.update_device_list()
    camera = arv.Camera.new(arv.get_device_id(0))
    stream = camera.create_stream(None, None)
    stream.push_buffer(arv.Buffer.new_allocate(1))
    camera.start_acquisition()
    buffer = stream.timeout_pop_buffer(1000)
    assert buffer.get_status() == arv.BufferStatus.SIZE_MISMATCH


# --- Against real Aravis --------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "real_name"),
    [
        (FakeArvCamera, "Camera"),
        (FakeArvDevice, "Device"),
        (FakeArvStream, "Stream"),
        (FakeArvBuffer, "Buffer"),
    ],
)
def test_fake_methods_exist_in_aravis(real, cls, real_name):
    real_cls = getattr(real, real_name)
    missing = [
        name
        for name in vars(cls)
        if not name.startswith("_")
        and callable(vars(cls)[name])
        and name not in ("arrive", "payload_for_format")
        and not hasattr(real_cls, name)
    ]
    assert missing == []


def test_fake_module_functions_exist_in_aravis(real):
    for name in (
        "update_device_list",
        "get_n_devices",
        "get_device_id",
        "get_device_serial_nbr",
        "get_device_model",
        "get_device_physical_id",
    ):
        assert hasattr(real, name), name


def test_pixel_format_codes_match_aravis(real):
    names = {
        "Mono8": "MONO_8",
        "Mono16": "MONO_16",
        "RGB8": "RGB_8_PACKED",
        "BayerGR8": "BAYER_GR_8",
        "BayerRG8": "BAYER_RG_8",
        "BayerGB8": "BAYER_GB_8",
        "BayerBG8": "BAYER_BG_8",
        "BayerGR16": "BAYER_GR_16",
        "BayerRG16": "BAYER_RG_16",
        "BayerGB16": "BAYER_GB_16",
        "BayerBG16": "BAYER_BG_16",
    }
    for code, fmt in aravis._PIXEL_FORMATS.items():
        assert getattr(real, f"PIXEL_FORMAT_{names[fmt]}") == code, fmt
    for fmt, code in PFNC.items():
        if fmt in names:
            assert getattr(real, f"PIXEL_FORMAT_{names[fmt]}") == code, fmt


def test_buffer_statuses_match_aravis(real):
    for status in ("SUCCESS", "MISSING_PACKETS", "SIZE_MISMATCH", "TIMEOUT"):
        assert hasattr(real.BufferStatus, status)
    assert hasattr(real.DeviceError, "TIMEOUT")
