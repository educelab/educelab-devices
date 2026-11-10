import math

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
