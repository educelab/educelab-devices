"""Preview done on the host: crop a frame to the region and stride it down."""

import math
from dataclasses import dataclass
from typing import Any, TypeVar

import numpy.typing as npt

from educelab.devices.cameras.base import PreviewGeometry, PreviewRegion

__all__ = ["HostCrop", "hardware_roi", "host_crop", "pick_decimation"]

A = TypeVar("A", bound=npt.NDArray[Any])

# Fractions times sizes pick up float error; don't let it cost a pixel.
_SNAP = 1e-6


@dataclass(frozen=True)
class HostCrop:
    """How to turn a full frame into a preview image.

    Attributes:
        geometry: What the preview reports: the region the crop covers and
            the image size after striding.
        rows: Rows of the frame to keep.
        cols: Columns of the frame to keep.
    """

    geometry: PreviewGeometry
    rows: slice
    cols: slice

    def apply(self, image: A) -> A:
        """The preview image from a frame, ``(height, width[, channels])``."""
        return image[self.rows, self.cols]  # type: ignore[return-value]


def host_crop(
    region: PreviewRegion,
    max_size: int | None,
    *,
    sensor: tuple[int, int],
    frame: tuple[int, int],
    origin: tuple[int, int] = (0, 0),
) -> HostCrop:
    """Plan a host-side preview of ``region`` from frames of size ``frame``.

    The region is rounded inward to whole pixels and clipped to the part of
    the sensor the frames cover, so the reported region lies within the
    requested one. Every n-th pixel is then kept, with n the smallest stride
    that brings the longest side within ``max_size``.

    Args:
        region: The requested region, in fractions of the sensor.
        max_size: Longest side of the preview image, or ``None`` for no cap.
        sensor: The whole sensor as ``(width, height)``.
        frame: The frames' size as ``(width, height)``.
        origin: Where the frames start on the sensor, as ``(x, y)`` pixels.

    Raises:
        ValueError: The region covers no whole pixel of the frames.
    """
    sensor_w, sensor_h = sensor
    x0, x1 = _span(region.x, region.width, sensor_w, origin[0], frame[0])
    y0, y1 = _span(region.y, region.height, sensor_h, origin[1], frame[1])
    if x1 <= x0 or y1 <= y0:
        raise ValueError(
            f"preview region {region} lies outside the frames this camera "
            f"delivers ({frame[0]} x {frame[1]} at {origin} on a {sensor_w} x "
            f"{sensor_h} sensor)"
        )
    width, height = x1 - x0, y1 - y0
    stride = 1 if max_size is None else max(1, math.ceil(max(width, height) / max_size))
    applied = PreviewRegion(
        x0 / sensor_w, y0 / sensor_h, width / sensor_w, height / sensor_h
    )
    geometry = PreviewGeometry(
        applied, math.ceil(width / stride), math.ceil(height / stride)
    )
    return HostCrop(
        geometry,
        slice(y0 - origin[1], y1 - origin[1], stride),
        slice(x0 - origin[0], x1 - origin[0], stride),
    )


def _span(
    start: float, size: float, sensor: int, origin: int, frame: int
) -> tuple[int, int]:
    """The whole sensor pixels a fractional span covers, clipped to the frame."""
    low = math.ceil(start * sensor - _SNAP)
    high = math.floor((start + size) * sensor + _SNAP)
    return max(low, origin), min(high, origin + frame)


def pick_decimation(
    region: PreviewRegion,
    max_size: int | None,
    *,
    full: tuple[int, int],
    choices: tuple[int, ...],
) -> int:
    """The largest decimation that still leaves ``max_size`` pixels on the
    region's longest side; 1 when nothing asks for a reduction.

    Args:
        full: The whole image at decimation 1, as ``(width, height)``.
        choices: The decimations the camera offers.
    """
    if max_size is None:
        return 1
    longest = max(region.width * full[0], region.height * full[1])
    return max((d for d in choices if d > 1 and longest / d >= max_size), default=1)


def hardware_roi(
    region: PreviewRegion,
    *,
    full: tuple[int, int],
    size_inc: tuple[int, int],
    offset_inc: tuple[int, int],
    min_size: tuple[int, int] = (1, 1),
) -> tuple[int, int, int, int]:
    """The camera ROI to show ``region``, as ``(x, y, width, height)`` pixels.

    The ROI lands on the camera's increments and contains the region, for the
    host to crop the rest. Its size depends only on the region's size, so
    moving a region never changes it, and never costs an acquisition restart.

    Args:
        full: The whole image at the current decimation, ``(width, height)``.
        size_inc: Increments of ``Width`` and ``Height``.
        offset_inc: Increments of ``OffsetX`` and ``OffsetY``.
        min_size: The smallest ``Width`` and ``Height``.
    """
    x, width = _roi_axis(
        region.x, region.width, full[0], size_inc[0], offset_inc[0], min_size[0]
    )
    y, height = _roi_axis(
        region.y, region.height, full[1], size_inc[1], offset_inc[1], min_size[1]
    )
    return x, y, width, height


def _roi_axis(
    start: float, size: float, full: int, size_inc: int, offset_inc: int, least: int
) -> tuple[int, int]:
    # Room for the offset rounding down by up to one increment.
    wanted = math.ceil(size * full - _SNAP) + offset_inc
    most = full // size_inc * size_inc
    length = min(max(math.ceil(wanted / size_inc) * size_inc, least), most)
    offset = math.floor(start * full + _SNAP) // offset_inc * offset_inc
    # Slide back from the far edge rather than shrink.
    offset = min(offset, (full - length) // offset_inc * offset_inc)
    return max(offset, 0), length
