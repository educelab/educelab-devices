"""Preview done on the host: crop a frame to the region and stride it down."""

import math
from dataclasses import dataclass
from typing import Any, TypeVar

import numpy.typing as npt

from educelab.devices.cameras.base import PreviewGeometry, PreviewRegion

__all__ = ["HostCrop", "host_crop"]

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
