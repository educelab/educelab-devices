import math

import numpy as np
import pytest

from educelab.devices.cameras import PreviewGeometry, PreviewRegion
from educelab.devices.cameras.base import check_max_size


def test_region_defaults_to_the_full_sensor():
    assert PreviewRegion() == PreviewRegion(0.0, 0.0, 1.0, 1.0)
    assert PreviewRegion().is_full


def test_region_accepts_a_box_inside_the_sensor():
    region = PreviewRegion(0.25, 0.5, 0.5, 0.5)
    assert (region.x, region.y, region.width, region.height) == (0.25, 0.5, 0.5, 0.5)
    assert not region.is_full


@pytest.mark.parametrize(
    "box",
    [
        (-0.1, 0.0, 0.5, 0.5),
        (0.0, 1.0, 0.5, 0.5),
        (0.0, 0.0, 0.0, 0.5),
        (0.0, 0.0, 0.5, -0.5),
        (0.6, 0.0, 0.5, 0.5),
        (0.0, 0.0, 1.0, 1.5),
        (math.nan, 0.0, 0.5, 0.5),
        (0.0, 0.0, math.inf, 0.5),
    ],
)
def test_region_outside_the_sensor_raises(box):
    with pytest.raises(ValueError, match="region"):
        PreviewRegion(*box)


def test_region_tolerates_float_error_at_the_far_edge():
    region = PreviewRegion(0.1, 0.7, 0.9, 0.3)
    assert region.x + region.width == pytest.approx(1.0)


@pytest.mark.parametrize("value", ["0", None, True])
def test_region_rejects_non_numbers(value):
    with pytest.raises(TypeError):
        PreviewRegion(value, 0.0, 0.5, 0.5)


def test_region_of_accepts_a_region_a_tuple_or_none():
    region = PreviewRegion(0.25, 0.25, 0.5, 0.5)
    assert PreviewRegion.of(region) is region
    assert PreviewRegion.of((0.25, 0.25, 0.5, 0.5)) == region
    assert PreviewRegion.of(None) == PreviewRegion()


@pytest.mark.parametrize("value", [(0.0, 0.0, 1.0), [0.0, 0.0, 1.0, 1.0, 1.0], "full"])
def test_region_of_rejects_other_shapes(value):
    with pytest.raises(TypeError, match="x, y, width, height"):
        PreviewRegion.of(value)


def test_region_contains_itself_and_smaller_boxes():
    outer = PreviewRegion(0.2, 0.2, 0.6, 0.6)
    assert outer.contains(outer)
    assert outer.contains(PreviewRegion(0.3, 0.3, 0.2, 0.2))
    assert not outer.contains(PreviewRegion(0.1, 0.3, 0.2, 0.2))
    assert not outer.contains(PreviewRegion())


def test_geometry_reports_region_and_output_size():
    geometry = PreviewGeometry(PreviewRegion(), 640, 480)
    assert (geometry.width, geometry.height) == (640, 480)
    assert geometry.region.is_full


@pytest.mark.parametrize("size", [(0, 480), (640, -1)])
def test_geometry_rejects_an_empty_output(size):
    with pytest.raises(ValueError, match="output"):
        PreviewGeometry(PreviewRegion(), *size)


@pytest.mark.parametrize("value", [None, 1, 1280])
def test_max_size_accepts_none_or_a_positive_int(value):
    assert check_max_size(value) == value


@pytest.mark.parametrize("value", [0, -5])
def test_max_size_rejects_non_positive_values(value):
    with pytest.raises(ValueError, match="max_size"):
        check_max_size(value)


@pytest.mark.parametrize("value", [1280.0, True, "1280"])
def test_max_size_rejects_non_ints(value):
    with pytest.raises(TypeError, match="max_size"):
        check_max_size(value)


# Host crop and stride (cameras/_preview.py)

from educelab.devices.cameras._preview import host_crop


def test_full_region_on_a_full_frame_keeps_every_pixel():
    crop = host_crop(PreviewRegion(), None, sensor=(640, 480), frame=(640, 480))
    assert crop.geometry == PreviewGeometry(PreviewRegion(), 640, 480)
    image = np.arange(480 * 640).reshape(480, 640)
    assert np.array_equal(crop.apply(image), image)


def test_max_size_strides_the_longest_side_under_the_cap():
    crop = host_crop(PreviewRegion(), 200, sensor=(640, 480), frame=(640, 480))
    assert (crop.geometry.width, crop.geometry.height) == (160, 120)
    image = np.zeros((480, 640, 3))
    assert crop.apply(image).shape == (120, 160, 3)


@pytest.mark.parametrize("max_size", [1, 7, 100, 333, 639, 640, 641])
def test_output_never_exceeds_max_size(max_size):
    crop = host_crop(PreviewRegion(), max_size, sensor=(640, 480), frame=(640, 480))
    out = crop.apply(np.zeros((480, 640)))
    assert max(out.shape) <= max_size
    assert out.shape == (crop.geometry.height, crop.geometry.width)


def test_region_is_rounded_inward_to_whole_pixels():
    region = PreviewRegion(0.1001, 0.2501, 0.5, 0.5)
    crop = host_crop(region, None, sensor=(1000, 100), frame=(1000, 100))
    assert region.contains(crop.geometry.region)
    assert crop.geometry.region == PreviewRegion(0.101, 0.26, 0.499, 0.49)
    assert (crop.geometry.width, crop.geometry.height) == (499, 49)


def test_region_is_clipped_to_a_frame_cropped_from_the_sensor():
    # The IDS's frames: 5112 x 3840 at offset (8, 8) on a 5136 x 3856 sensor.
    crop = host_crop(
        PreviewRegion(), None, sensor=(5136, 3856), frame=(5112, 3840), origin=(8, 8)
    )
    region = crop.geometry.region
    assert region.x == pytest.approx(8 / 5136)
    assert region.width == pytest.approx(5112 / 5136)
    image = np.zeros((3840, 5112))
    assert crop.apply(image).shape == (3840, 5112)


def test_crop_is_relative_to_the_frame_origin():
    crop = host_crop(
        PreviewRegion(0.5, 0.5, 0.5, 0.5),
        None,
        sensor=(20, 20),
        frame=(16, 16),
        origin=(2, 2),
    )
    image = np.arange(16 * 16).reshape(16, 16)
    # Sensor pixels 10..17 are frame pixels 8..15.
    assert np.array_equal(crop.apply(image), image[8:16, 8:16])


def test_region_outside_the_frame_raises():
    with pytest.raises(ValueError, match="outside the frames"):
        host_crop(
            PreviewRegion(0.0, 0.0, 0.001, 0.001),
            None,
            sensor=(5136, 3856),
            frame=(5112, 3840),
            origin=(8, 8),
        )
