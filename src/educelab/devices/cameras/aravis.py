"""USB3 Vision cameras through Aravis, on Linux and macOS.

Aravis is an open-source GenICam library that reaches USB3 Vision cameras
through libusb, so no manufacturer SDK is needed. Python reaches it through
PyGObject (``gi``), which is imported on first use, so this module imports
without it. Neither is on PyPI in a form pip can install alone; see
``docs/hardware/usb3-vision.md`` for the steps on each OS.
"""

import importlib
import logging
import math
import threading
from collections.abc import Callable
from types import ModuleType
from typing import Any, TypeVar

import numpy as np
import numpy.typing as npt

from educelab.devices.cameras._genicam import BufferStatus, GenICamCamera
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import (
    DeviceBusy,
    DeviceError,
    DeviceTimeout,
    import_optional,
)

__all__ = [
    "ARAVIS_INSTALL_HINT",
    "ARAVIS_VERSION",
    "BUFFER_COUNT",
    "AravisCamera",
    "load_aravis",
]

logger = logging.getLogger(__name__)

BACKEND = "aravis"

T = TypeVar("T")

ARAVIS_VERSION = "0.8"
"""The Aravis API version the driver asks PyGObject for (the stable series)."""

ARAVIS_INSTALL_HINT = (
    f"The Aravis driver needs Aravis {ARAVIS_VERSION} and PyGObject. On macOS: "
    "brew install aravis pkgconf cairo, then pip install pycairo, then pip install "
    "PyGObject. On Linux (Ubuntu): apt install gir1.2-aravis-0.8 and python3-gi (or "
    "build PyGObject in a venv), plus the udev rule. See docs/hardware/usb3-vision.md: "
    "https://github.com/educelab/educelab-devices/blob/main/docs/hardware/"
    "usb3-vision.md"
)

BUFFER_COUNT = 4
"""Buffers in each stream. A 20 MP Mono8 frame is about 20 MB, so a few is
plenty; Aravis drops frames when none is free (usb3-vision.md, Newest only)."""

# PFNC codes of the formats the core can shape, as in Aravis's arvenums.h.
_PIXEL_FORMATS = {
    0x01080001: "Mono8",
    0x01100007: "Mono16",
    0x02180014: "RGB8",
    0x01080008: "BayerGR8",
    0x01080009: "BayerRG8",
    0x0108000A: "BayerGB8",
    0x0108000B: "BayerBG8",
    0x0110002E: "BayerGR16",
    0x0110002F: "BayerRG16",
    0x01100030: "BayerGB16",
    0x01100031: "BayerBG16",
}

_DEVICE_ERROR_DOMAIN = "arv-device-error-quark"

# Aravis is not thread safe across interfaces, and device discovery goes
# through them (usb3-vision.md, Thread safety).
_system_lock = threading.Lock()


def load_aravis() -> ModuleType:
    """Import ``gi.repository`` with Aravis loaded, or raise an `ImportError`
    that explains how to install it."""
    gi = import_optional("gi", hint=ARAVIS_INSTALL_HINT)
    try:
        gi.require_version("Aravis", ARAVIS_VERSION)
    except ValueError as exc:
        raise ImportError(
            f"Could not load Aravis {ARAVIS_VERSION}: {exc}. {ARAVIS_INSTALL_HINT}",
            name="Aravis",
        ) from exc
    repository = import_optional("gi.repository", hint=ARAVIS_INSTALL_HINT)
    import_optional("gi.repository.Aravis", hint=ARAVIS_INSTALL_HINT)
    importlib.import_module("gi.repository.GLib")
    return repository


class AravisCamera(GenICamCamera):
    """One USB3 Vision camera, addressed by serial number, through Aravis.

    Behaves like every GenICam camera in this package (`GenICamCamera`, ADR
    0006). Aravis has no newest-only buffering, so the driver keeps a small
    pool of buffers and hands back only the newest frame, recycling the rest.

    Aravis errors become `DeviceError`s and aren't chained to the original
    ``GLib.Error``.

    Args:
        serial: The camera's serial number, as `discover()` reports it.
        repository: An object to use in place of ``gi.repository`` (with
            ``Aravis`` and ``GLib`` attributes), for tests.
    """

    label = "Aravis"

    def __init__(self, serial: str, *, repository: ModuleType | None = None) -> None:
        super().__init__(serial, _AravisBackend(repository))

    @classmethod
    def discover(cls) -> list[DeviceInfo]:
        """List attached Aravis cameras without opening them.

        Raises:
            ImportError: Aravis or PyGObject isn't installed.
        """
        return _AravisBackend(None).discover()


class _AravisBackend:
    """Finds and opens cameras through Aravis's device list."""

    name = BACKEND

    def __init__(self, repository: ModuleType | None) -> None:
        self._repository = repository

    def _load(self) -> tuple[Any, Any]:
        repository = self._repository or load_aravis()
        return repository.Aravis, repository.GLib

    def discover(self) -> list[DeviceInfo]:
        arv, _ = self._load()
        with _system_lock:
            arv.update_device_list()
            return [
                DeviceInfo(
                    BACKEND,
                    arv.get_device_model(i) or "Aravis camera",
                    address=arv.get_device_physical_id(i),
                    serial_number=arv.get_device_serial_nbr(i),
                )
                for i in range(arv.get_n_devices())
            ]

    def open(self, serial: str) -> "_AravisConnection | None":
        arv, glib = self._load()
        with _system_lock:
            # Updating the list waits about a second on GigE discovery, so try
            # the last listing first (usb3-vision.md, Timing).
            for refreshed in (False, True):
                if refreshed:
                    arv.update_device_list()
                device_id = _device_id(arv, serial)
                if device_id is None:
                    continue
                try:
                    return _AravisConnection(arv, glib, arv.Camera.new(device_id))
                except (glib.Error, TypeError) as exc:
                    if not refreshed:
                        continue
                    # Listed but unopenable: another process has claimed it
                    # (assumed; usb3-vision.md).
                    error = DeviceBusy(f"{device_id} is listed but won't open: {exc}")
                    break
            else:
                return None
        raise error


def _device_id(arv: Any, serial: str) -> str | None:
    """The ID of the camera with this serial in Aravis's last listing."""
    for i in range(arv.get_n_devices()):
        if arv.get_device_serial_nbr(i) == serial:
            return str(arv.get_device_id(i))
    return None


class _AravisConnection:
    """A `Connection` onto one open Aravis camera."""

    def __init__(self, arv: Any, glib: Any, camera: Any) -> None:
        self._arv = arv
        self._glib = glib
        self._camera: Any = camera
        self._device: Any = camera.get_device()
        self._stream: Any = None
        self._payload = 0
        self._acquiring = False
        # Without these, Aravis sends out-of-range or read-only writes straight
        # to the camera (usb3-vision.md, Camera and device).
        self._device.set_range_check_policy(arv.RangeCheckPolicy.ENABLE)
        self._device.set_access_check_policy(arv.AccessCheckPolicy.ENABLE)
        self._allocate()

    def _do(self, fn: Callable[[], T]) -> T:
        try:
            return fn()
        except self._glib.Error as exc:
            kind: type[DeviceError] = DeviceError
            timeout = int(self._arv.DeviceError.TIMEOUT)
            if exc.domain == _DEVICE_ERROR_DOMAIN and exc.code == timeout:
                kind = DeviceTimeout
            error = kind(str(exc))
        del fn
        raise error

    def _allocate(self) -> None:
        """Give the camera a new stream with buffers sized to its payload."""
        payload = self._do(self._camera.get_payload)
        stream = self._do(lambda: self._camera.create_stream(None, None))
        for _ in range(BUFFER_COUNT):
            stream.push_buffer(self._arv.Buffer.new_allocate(payload))
        self._stream, self._payload = stream, payload

    # --- Node map ---------------------------------------------------------------

    def has(self, name: str) -> bool:
        return self._device.get_feature(name) is not None

    def _access(self, name: str) -> Any:
        if not self.has(name):
            return None
        if not self._do(lambda: self._device.is_feature_available(name)):
            return None
        return self._do(lambda: self._device.get_feature_access_mode(name))

    def is_readable(self, name: str) -> bool:
        modes = self._arv.GcAccessMode
        return self._access(name) in (modes.RO, modes.RW)

    def is_writable(self, name: str) -> bool:
        modes = self._arv.GcAccessMode
        return self._access(name) in (modes.WO, modes.RW)

    def get_float(self, name: str) -> float:
        return float(self._do(lambda: self._device.get_float_feature_value(name)))

    def get_int(self, name: str) -> int:
        return int(self._do(lambda: self._device.get_integer_feature_value(name)))

    def get_string(self, name: str) -> str:
        return str(self._do(lambda: self._device.get_string_feature_value(name)))

    def get_enum(self, name: str) -> str:
        return self.get_string(name)

    def float_range(self, name: str) -> tuple[float, float]:
        low, high = self._do(lambda: self._device.get_float_feature_bounds(name))
        return float(low), float(high)

    def int_range(self, name: str) -> tuple[int, int, int]:
        device = self._device
        low, high = self._do(lambda: device.get_integer_feature_bounds(name))
        inc = self._do(lambda: device.get_integer_feature_increment(name))
        return int(low), int(high), int(inc)

    def enum_entries(self, name: str) -> tuple[str, ...]:
        device = self._device
        entries = self._do(
            lambda: device.dup_available_enumeration_feature_values_as_strings(name)
        )
        return tuple(str(e) for e in entries)

    def set_float(self, name: str, value: float) -> None:
        self._do(lambda: self._device.set_float_feature_value(name, value))

    def set_int(self, name: str, value: int) -> None:
        self._do(lambda: self._device.set_integer_feature_value(name, value))

    def set_enum(self, name: str, symbol: str) -> None:
        self._do(lambda: self._device.set_string_feature_value(name, symbol))

    def execute(self, name: str) -> None:
        self._do(lambda: self._device.execute_command(name))

    # --- Stream -----------------------------------------------------------------

    def start_acquisition(self) -> None:
        # A new pixel format may need bigger buffers than the pool has.
        if self._do(self._camera.get_payload) != self._payload:
            self._allocate()
        self._do(self._camera.start_acquisition)
        self._acquiring = True

    def stop_acquisition(self) -> None:
        if not self._acquiring:
            return
        self._do(self._camera.stop_acquisition)
        self._acquiring = False
        # Frames from before the stop may be in another format; drop them.
        while (buffer := self._stream.try_pop_buffer()) is not None:
            self._stream.push_buffer(buffer)

    def grab(self, timeout: float | None) -> "_AravisBuffer":
        if not self._acquiring:
            raise DeviceError("the camera isn't acquiring")
        stream = self._stream
        buffer = stream.try_pop_buffer()
        if buffer is None and timeout != 0:
            if timeout is None:
                buffer = stream.pop_buffer()
            else:
                buffer = stream.timeout_pop_buffer(max(1, math.ceil(timeout * 1e6)))
        if buffer is None:
            raise DeviceTimeout(f"no frame in {timeout:g} s")
        # Newest only: recycle every older frame already waiting.
        while (newer := stream.try_pop_buffer()) is not None:
            stream.push_buffer(buffer)
            buffer = newer
        return _AravisBuffer(self, buffer)

    def close(self) -> None:
        if self._camera is None:
            return
        problem = None
        try:
            self.stop_acquisition()
        except DeviceError as exc:
            problem = f"stop acquisition: {exc}"
        # Aravis lets go of the camera when the last reference to it goes.
        self._stream = self._device = self._camera = None
        if problem:
            raise DeviceError(problem)


class _AravisBuffer:
    """A `Buffer` over one ``Aravis.Buffer``, pushed back to its stream on release."""

    def __init__(self, conn: _AravisConnection, buffer: Any) -> None:
        self._conn = conn
        self._stream = conn._stream
        self._buffer = buffer

    def status(self) -> BufferStatus:
        status = self._buffer.get_status()
        if status == self._conn._arv.BufferStatus.SUCCESS:
            return BufferStatus.COMPLETE
        return BufferStatus.INCOMPLETE

    def status_text(self) -> str:
        status = self._buffer.get_status()
        return str(getattr(status, "value_nick", status))

    def device_timestamp_ns(self) -> int:
        return int(self._buffer.get_timestamp())

    def frame_id(self) -> int:
        return int(self._buffer.get_frame_id())

    def pixel_format(self) -> str:
        code = int(self._buffer.get_image_pixel_format())
        try:
            return _PIXEL_FORMATS[code]
        except KeyError:
            raise DeviceError(f"unsupported pixel format 0x{code:08x}") from None

    def pixels(self) -> npt.NDArray[Any]:
        fmt = self.pixel_format()
        dtype = np.uint16 if fmt.endswith("16") else np.uint8
        shape: tuple[int, ...] = (
            self._buffer.get_image_height(),
            self._buffer.get_image_width(),
        )
        if fmt == "RGB8":
            shape += (3,)
        data = self._buffer.get_data()
        return np.frombuffer(data, dtype=dtype).reshape(shape).copy()

    def release(self) -> None:
        self._stream.push_buffer(self._buffer)
