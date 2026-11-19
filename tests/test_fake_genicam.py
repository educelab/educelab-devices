"""The in-memory GenICam backend that the shared core is tested against."""

import numpy as np
import pytest

from contracts.genicam import BackendContract
from educelab.devices.cameras._genicam import Backend, Connection
from fakes.genicam import (
    FakeBackend,
    FakeCamera,
    FakeCameraError,
    cm3_31s4c,
    frame_bytes,
    payload_size,
)


class TestFakeBackend(BackendContract):
    @pytest.fixture
    def make_backend(self):
        return FakeBackend


def test_the_fake_fits_the_seam() -> None:
    # For mypy: the assignments fail type-checking if the fake drifts.
    backend: Backend = FakeBackend([FakeCamera()])
    connection: Connection | None = backend.open(FakeCamera().serial)
    assert connection is not None
    connection.close()


# --- FakeCamera rules the contract doesn't reach ------------------------------


@pytest.fixture
def opened():
    camera = FakeCamera()
    camera.open()
    return camera


def triggered(camera):
    camera.write("TriggerSource", "Software")
    camera.write("TriggerMode", "On")
    camera.start()
    return camera


def test_pixel_format_is_read_only_while_acquiring(opened):
    opened.start()
    assert opened.access("PixelFormat") == "RO"
    with pytest.raises(FakeCameraError):
        opened.write("PixelFormat", "Mono16")
    opened.stop()
    opened.write("PixelFormat", "Mono16")
    assert opened.get("PixelFormat") == "Mono16"


def test_timestamp_latch_follows_the_host_clock():
    now = [1_000_000.0]
    camera = FakeCamera(clock=lambda: now[0], uptime=50.0)
    camera.open()
    camera.execute("TimestampLatch")
    assert camera.read("Timestamp") == 50_000_000_000
    now[0] += 1.5
    camera.execute("TimestampLatch")
    assert camera.read("Timestamp") == 51_500_000_000


def test_triggers_can_be_ignored_by_number(opened):
    triggered(opened).ignored_trigger_calls = {2}
    for _ in range(3):
        opened.execute("TriggerSoftware")
    assert opened.trigger_calls == 3
    assert len(opened.pending_triggers) == 2


# --- The color CM3-U3-31S4C ---------------------------------------------------


@pytest.fixture
def cm3():
    camera = cm3_31s4c()
    camera.open()
    return camera


def test_the_color_cm3_opens_in_bayer_with_white_balance_on_auto(cm3):
    assert cm3.get("PixelFormat") == "BayerRG8"
    assert cm3.get("PixelColorFilter") == "BayerRG"
    assert cm3.get("BalanceWhiteAuto") == "Continuous"
    assert cm3.access("BalanceRatio") == "RO"


def test_the_balance_ratio_follows_its_selector(cm3):
    cm3.write("BalanceWhiteAuto", "Off")
    cm3.write("BalanceRatioSelector", "Blue")
    cm3.write("BalanceRatio", 0.5)
    cm3.write("BalanceRatioSelector", "Red")
    assert cm3.read("BalanceRatio") == 2.0
    cm3.write("BalanceRatio", 3.0)
    cm3.write("BalanceRatioSelector", "Blue")
    assert cm3.read("BalanceRatio") == 0.5
    assert cm3.nodes["BalanceRatio"].value == {"Red": 3.0, "Blue": 0.5}


def test_balance_ratios_are_writable_only_with_auto_off(cm3):
    with pytest.raises(FakeCameraError):
        cm3.write("BalanceRatio", 1.0)
    cm3.write("BalanceWhiteAuto", "Once")
    assert cm3.access("BalanceRatio") == "RO"
    cm3.write("BalanceWhiteAuto", "Off")
    cm3.write("BalanceRatio", 1.0)
    with pytest.raises(FakeCameraError):
        cm3.write("BalanceRatio", 4.5)


def test_white_balance_and_sharpening_follow_the_pixel_format(cm3):
    expected = {
        # format: (white balance, sharpening)
        "Mono8": ("NA", "RW"),
        "Mono12p": ("NA", "RW"),
        "BayerRG8": ("RW", "NA"),
        "BayerRG16": ("RW", "NA"),
        "YCbCr422_8_CbYCrY": ("RW", "RW"),
        "RGB8": ("RW", "RW"),
    }
    for fmt, (balance, sharpening) in expected.items():
        cm3.write("PixelFormat", fmt)
        assert cm3.access("BalanceWhiteAuto") == balance, fmt
        assert cm3.access("BalanceRatioSelector") == balance, fmt
        assert cm3.access("SharpnessEnabled") == sharpening, fmt
        assert cm3.get("PixelColorFilter") == "BayerRG"
    cm3.write("PixelFormat", "Mono8")
    assert cm3.access("BalanceRatio") == "NA"


@pytest.mark.parametrize(
    ("fmt", "per_pixel"),
    [
        ("Mono8", 1),
        ("Mono12Packed", 1.5),
        ("Mono12p", 1.5),
        ("Mono16", 2),
        ("BayerRG8", 1),
        ("BayerRG16", 2),
        ("YCbCr411_8_CbYYCrYY", 1.5),
        ("YCbCr422_8_CbYCrY", 2),
        ("YCbCr8_CbYCr", 3),
        ("RGB8", 3),
    ],
)
def test_each_format_sends_a_payload_of_its_size(cm3, fmt, per_pixel):
    cm3.write("PixelFormat", fmt)
    cm3.start()
    frame = cm3.next_frame()
    expected = int(cm3.width * cm3.height * per_pixel)
    assert payload_size(cm3) == expected
    assert len(frame_bytes(frame.pixels, fmt)) == expected


def test_12_bit_packings_put_the_bits_where_each_standard_says():
    pixels = np.array([[0xABC, 0x123]], dtype=np.uint16)
    # GigE Vision's Mono12Packed: high bytes 0 and 2, low nibbles in byte 1.
    assert frame_bytes(pixels, "Mono12Packed") == bytes([0xAB, 0x3C, 0x12])
    # The PFNC's Mono12p: LSB first.
    assert frame_bytes(pixels, "Mono12p") == bytes([0xBC, 0x3A, 0x12])
    assert frame_bytes(pixels, "BayerGB12Packed") == bytes([0xAB, 0x3C, 0x12])
    assert frame_bytes(pixels, "BayerBG12p") == bytes([0xBC, 0x3A, 0x12])
