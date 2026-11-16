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

from educelab.devices.cameras._preview import (
    hardware_roi,
    host_crop,
    pick_decimation,
    reduction,
)


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


# --- Hardware ROI -------------------------------------------------------------

CM3 = {"full": (2448, 2048), "size_inc": (8, 2), "offset_inc": (2, 2)}


def covers(roi, region, full):
    x, y, width, height = roi
    return (
        x <= region.x * full[0] + 1e-6
        and y <= region.y * full[1] + 1e-6
        and x + width >= (region.x + region.width) * full[0] - 1e-6
        and y + height >= (region.y + region.height) * full[1] - 1e-6
    )


def test_roi_of_the_whole_sensor_is_the_whole_image():
    assert hardware_roi(PreviewRegion(), **CM3) == (0, 0, 2448, 2048)


def test_roi_lands_on_the_increments_and_contains_the_region():
    region = PreviewRegion(0.301, 0.107, 0.25, 0.25)
    x, y, width, height = roi = hardware_roi(region, **CM3)
    assert (x % 2, y % 2, width % 8, height % 2) == (0, 0, 0, 0)
    assert covers(roi, region, CM3["full"])
    assert width <= 0.25 * 2448 + 2 + 8 and height <= 0.25 * 2048 + 2 + 2


def test_moving_a_region_never_changes_the_roi_size():
    sizes = set()
    for i in range(200):
        region = PreviewRegion(0.75 * i / 199, 0.75 * (199 - i) / 199, 0.25, 0.25)
        roi = hardware_roi(region, **CM3)
        assert covers(roi, region, CM3["full"])
        sizes.add(roi[2:])
    assert len(sizes) == 1


def test_roi_at_the_far_edge_slides_back_inside_the_image():
    x, y, width, height = hardware_roi(PreviewRegion(0.75, 0.75, 0.25, 0.25), **CM3)
    assert x + width <= 2448 and y + height <= 2048
    assert covers(
        (x, y, width, height), PreviewRegion(0.75, 0.75, 0.25, 0.25), (2448, 2048)
    )


def test_roi_is_no_smaller_than_the_camera_allows():
    roi = hardware_roi(PreviewRegion(0.5, 0.5, 0.001, 0.001), **CM3, min_size=(64, 32))
    assert roi[2:] == (64, 32)


def test_roi_on_an_image_off_the_size_increment_stays_inside_it():
    # An image 1,222 wide can't be covered in steps of 8; the host crop clips.
    x, _, width, _ = hardware_roi(
        PreviewRegion(), full=(1222, 1024), size_inc=(8, 2), offset_inc=(2, 2)
    )
    assert (x, width) == (0, 1216)


@pytest.mark.parametrize(
    ("max_size", "expected"),
    [(None, 1), (4000, 1), (2448, 1), (1224, 2), (1000, 2), (612, 4), (64, 4)],
)
def test_decimation_never_goes_below_max_size(max_size, expected):
    picked = pick_decimation(
        PreviewRegion(), max_size, full=(2448, 2048), choices=(1, 2, 4)
    )
    assert picked == expected


def test_decimation_goes_by_the_region_not_the_sensor():
    region = PreviewRegion(0.25, 0.25, 0.5, 0.5)
    assert pick_decimation(region, 1000, full=(2448, 2048), choices=(1, 2)) == 1
    assert pick_decimation(region, 612, full=(2448, 2048), choices=(1, 2)) == 2


@pytest.mark.parametrize(
    ("longest", "max_size", "min_size", "step"),
    [
        (1000, None, None, 1),
        (1000, 300, None, 4),
        # The coarsest step that keeps the floor: 1024 into 880 stays whole,
        # where a cap of 880 would halve it to 512.
        (1024, None, 880, 1),
        (1024, 880, None, 2),
        (2448, None, 880, 2),
        (5136, None, 1000, 5),
        # A side shorter than the floor is never reduced.
        (300, None, 1000, 1),
        # The cap wins over the floor.
        (2448, 600, 500, 5),
    ],
)
def test_reduction_keeps_the_floor_under_the_cap(longest, max_size, min_size, step):
    assert reduction(longest, max_size, min_size) == step
    kept = math.ceil(longest / step)
    if max_size is not None:
        assert kept <= max_size


@pytest.mark.parametrize("min_size", [1, 7, 100, 333, 880, 1000, 1023, 1024, 4000])
def test_reduction_is_the_coarsest_that_keeps_the_floor(min_size):
    longest = 1024
    step = reduction(longest, None, min_size)
    assert math.ceil(longest / step) >= min(min_size, longest)
    assert math.ceil(longest / (step + 1)) < min_size or step == longest


def test_host_crop_keeps_at_least_min_size():
    crop = host_crop(
        PreviewRegion(), None, sensor=(2448, 2048), frame=(2448, 2048), min_size=880
    )
    assert (crop.geometry.width, crop.geometry.height) == (1224, 1024)


def test_decimation_keeps_min_size_where_one_is_given():
    full = (2448, 2048)
    # A cap of 880 decimates by 2; a floor of 880 does too, and no further.
    assert (
        pick_decimation(
            PreviewRegion(), None, full=full, choices=(1, 2, 4), min_size=880
        )
        == 2
    )
    assert (
        pick_decimation(
            PreviewRegion(), None, full=full, choices=(1, 2, 4), min_size=500
        )
        == 4
    )
    assert (
        pick_decimation(PreviewRegion(), None, full=full, choices=(1, 2), min_size=1300)
        == 1
    )
