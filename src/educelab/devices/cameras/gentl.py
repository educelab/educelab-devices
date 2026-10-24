"""GenICam cameras through a vendor's GenTL producer, on Windows and Linux.

A GenTL producer is a ``.cti`` library from a camera vendor (e.g. IDS peak's
``ids_u3vgentl.cti``) that moves frames from the camera to the host. The driver
loads it with EMVA's ``genicam`` package (the ``[gentl]`` extra), which is
imported on first use, so this module imports without it. Producers are found
in the folders listed in ``GENICAM_GENTL64_PATH``. See
``docs/hardware/usb3-vision.md`` for installing a producer on each OS.
"""

import contextlib
import functools
import importlib
import io
import logging
import math
import os
import sys
import tempfile
import threading
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, TypeVar
from urllib.parse import unquote, urlparse

import numpy.typing as npt

from educelab.devices.cameras._genicam import (
    BufferStatus,
    GenICamCamera,
    pfnc_name,
    shaped_pixels,
)
from educelab.devices.device import DeviceInfo
from educelab.devices.errors import (
    DeviceBusy,
    DeviceError,
    DeviceTimeout,
    import_optional,
)

__all__ = [
    "BUFFER_COUNT",
    "GENTL_INSTALL_HINT",
    "PRODUCER_PATH_VARIABLE",
    "GenTLCamera",
    "find_producers",
    "load_genicam",
]

logger = logging.getLogger(__name__)

BACKEND = "gentl"

T = TypeVar("T")

PRODUCER_PATH_VARIABLE = "GENICAM_GENTL64_PATH"
"""The environment variable listing folders that hold GenTL producers."""

_INSTALL_DOCS = (
    "See docs/hardware/usb3-vision.md: https://github.com/educelab/educelab-devices/"
    "blob/main/docs/hardware/usb3-vision.md"
)
GENTL_INSTALL_HINT = (
    "The GenTL driver needs the genicam package (pip install "
    "'educelab-devices[gentl]') and a vendor GenTL producer, e.g. IDS peak's "
    f"ids_u3vgentl.cti, in a folder listed in {PRODUCER_PATH_VARIABLE}. "
    f"{_INSTALL_DOCS}"
)

BUFFER_COUNT = 4
"""Buffers announced to each stream (at least the producer's minimum). A 20 MP
Mono8 frame is about 20 MB, so a few is plenty."""

# genicam holds the GIL while it waits for a buffer (harvesters issue #508), so
# long waits are cut into slices this long, letting other threads run between.
_WAIT_SLICE_MS = 100
# How long the producer may take to list interfaces and devices. USB3 Vision
# enumeration is local; this is a loose guess (assumed; usb3-vision.md).
_LIST_TIMEOUT_MS = 500


def load_genicam() -> ModuleType:
    """Import ``genicam`` with its ``gentl`` and ``genapi`` modules, or raise an
    `ImportError` that names the extra to install."""
    genicam = import_optional("genicam", extra="gentl")
    importlib.import_module("genicam.gentl")
    importlib.import_module("genicam.genapi")
    return genicam


def find_producers() -> list[str]:
    """The ``.cti`` files in the folders listed in ``GENICAM_GENTL64_PATH``."""
    found: list[str] = []
    for entry in os.environ.get(PRODUCER_PATH_VARIABLE, "").split(os.pathsep):
        if not entry:
            continue
        path = Path(entry)
        if path.is_file() and path.suffix.lower() == ".cti":
            found.append(str(path))
        elif path.is_dir():
            found.extend(
                str(p) for p in sorted(path.iterdir()) if p.suffix.lower() == ".cti"
            )
    return found


class GenTLCamera(GenICamCamera):
    """One GenICam camera, addressed by serial number, through a GenTL producer.

    Behaves like every GenICam camera in this package (`GenICamCamera`, ADR
    0006). Cameras on the same producer share it; the last to close unloads it.

    Producer and ``genicam`` errors become `DeviceError`s and aren't chained to
    the originals.

    Args:
        serial: The camera's serial number, as `discover()` reports it.
        producer: The ``.cti`` file to use. By default every producer found in
            ``GENICAM_GENTL64_PATH`` is searched.
        genicam: A module to use in place of ``genicam``, for tests.
    """

    label = "GenTL"

    def __init__(
        self,
        serial: str,
        *,
        producer: str | os.PathLike[str] | None = None,
        genicam: ModuleType | None = None,
    ) -> None:
        producers = None if producer is None else [os.fspath(producer)]
        super().__init__(serial, _GenTLBackend(genicam, producers))

    @classmethod
    def discover(
        cls,
        producer: str | os.PathLike[str] | None = None,
        *,
        genicam: ModuleType | None = None,
    ) -> list[DeviceInfo]:
        """List the cameras GenTL producers can find, without opening them.

        Args:
            producer: The ``.cti`` file to use. By default every producer found
                in ``GENICAM_GENTL64_PATH`` is searched.
            genicam: A module to use in place of ``genicam``, for tests.

        Raises:
            ImportError: ``genicam`` isn't installed, or no producer is found or
                loads.
        """
        producers = None if producer is None else [os.fspath(producer)]
        return _GenTLBackend(genicam, producers).discover()


def _gentl_errors(genicam: Any) -> tuple[type[BaseException], ...]:
    return (genicam.gentl.GenericException, genicam.genapi.GenericException)


def _translate(genicam: Any, fn: Callable[[], T]) -> T:
    """Call into genicam, turning its errors into `DeviceError`s.

    Raised outside the ``except`` block, so the original isn't chained.
    """
    timeouts = (genicam.gentl.TimeoutException, genicam.genapi.TimeoutException)
    try:
        return fn()
    except _gentl_errors(genicam) as exc:
        kind = DeviceTimeout if isinstance(exc, timeouts) else DeviceError
        error = kind(str(exc) or type(exc).__name__)
    del fn
    raise error


class _Producer:
    """One loaded ``.cti``, with its system and interfaces, shared by every camera
    that uses it."""

    def __init__(self, genicam: Any, path: str) -> None:
        gentl = genicam.gentl
        self.genicam = genicam
        self.path = path
        self.users = 0
        self.interfaces: dict[str, Any] = {}
        # Some producers (FLIR's, verified) let one process open a camera twice;
        # this refuses it, as other backends do.
        self.open_serials: set[str] = set()
        try:
            self.producer = gentl.GenTLProducer.create_producer()
            with _stderr_to_log(path):
                self.producer.open(path)
        except gentl.GenericException as exc:
            error = ImportError(f"could not load {path}: {exc}")
        else:
            try:
                self.system = _translate(genicam, self.producer.create_system)
                _translate(genicam, self.system.open)
            except DeviceError as exc:
                # An ImportError, so callers skip this producer like one that
                # won't load; it's unloaded first so it isn't left loaded.
                with contextlib.suppress(DeviceError):
                    _translate(genicam, self.producer.close)
                error = ImportError(f"could not open {path}'s system: {exc}")
            else:
                return
        raise error

    def devices(self) -> list[Any]:
        """Device infos from every interface, updating the lists first."""
        found = []
        _translate(
            self.genicam,
            lambda: self.system.update_interface_info_list(_LIST_TIMEOUT_MS),
        )
        for info in self.system.interface_info_list:
            interface = self.interfaces.get(info.id_)
            if interface is None:
                interface = _translate(self.genicam, info.create_interface)
                _translate(self.genicam, interface.open)
                self.interfaces[info.id_] = interface
            update = functools.partial(
                interface.update_device_info_list, _LIST_TIMEOUT_MS
            )
            _translate(self.genicam, update)
            found.extend(interface.device_info_list)
        return found

    def close(self) -> None:
        problems = []
        closables = [*self.interfaces.values(), self.system, self.producer]
        for module in closables:
            try:
                _translate(self.genicam, module.close)
            except DeviceError as exc:
                problems.append(str(exc))
        self.interfaces.clear()
        if problems:
            raise DeviceError(f"could not unload {self.path}: {'; '.join(problems)}")


@contextlib.contextmanager
def _stderr_to_log(path: str) -> Iterator[None]:
    """Log what native code writes to the stderr descriptor, rather than show it.

    genicam reports each optional GenTL function an older producer lacks this
    way as the producer loads (e.g. "does not implement DSGetNumFlows" from
    FLIR's). Output from other threads in that moment is logged too.
    """
    if sys.stderr is not None:
        sys.stderr.flush()
    try:
        saved = os.dup(2)
    except OSError:
        # No stderr descriptor (e.g. under pythonw), so nothing to capture.
        yield
        return
    with tempfile.TemporaryFile() as captured:
        os.dup2(captured.fileno(), 2)
        try:
            yield
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            captured.seek(0)
            for line in captured.read().decode(errors="replace").splitlines():
                logger.debug("%s: %s", path, line)


# One entry per (genicam module, producer path); the module is a fake per test.
_producers_lock = threading.RLock()
_producers: dict[tuple[int, str], _Producer] = {}


def _acquire(genicam: Any, path: str) -> _Producer:
    with _producers_lock:
        entry = _producers.get((id(genicam), path))
        if entry is None:
            entry = _producers[(id(genicam), path)] = _Producer(genicam, path)
            logger.debug("loaded GenTL producer %s", path)
        entry.users += 1
        return entry


def _release(entry: _Producer) -> None:
    with _producers_lock:
        entry.users -= 1
        if entry.users:
            return
        del _producers[(id(entry.genicam), entry.path)]
        entry.close()
        logger.debug("unloaded GenTL producer %s", entry.path)


class _GenTLBackend:
    """Finds and opens cameras through one or more GenTL producers."""

    name = BACKEND

    def __init__(self, genicam: ModuleType | None, producers: list[str] | None) -> None:
        self._genicam = genicam
        self._producers = producers

    def _load(self) -> tuple[Any, list[str]]:
        genicam = self._genicam or load_genicam()
        producers = self._producers if self._producers is not None else find_producers()
        if not producers:
            raise ImportError(
                f"No GenTL producer found in {PRODUCER_PATH_VARIABLE} "
                f"({os.environ.get(PRODUCER_PATH_VARIABLE) or 'not set'}). "
                f"{GENTL_INSTALL_HINT}"
            )
        return genicam, producers

    def discover(self) -> list[DeviceInfo]:
        genicam, producers = self._load()
        found: list[DeviceInfo] = []
        with _producers_lock:
            usable = _Usable()
            for path in producers:
                listed = usable.list(genicam, path)
                if listed is None:
                    continue
                entry, infos = listed
                found.extend(
                    DeviceInfo(
                        BACKEND,
                        info.model or "GenTL camera",
                        address=info.id_,
                        serial_number=info.serial_number,
                        vendor=info.vendor or None,
                    )
                    for info in infos
                )
                _release_quietly(entry)
            usable.check()
        return found

    def open(self, serial: str) -> "_GenTLConnection | None":
        genicam, producers = self._load()
        gentl = genicam.gentl
        busy = (
            gentl.ResourceInUseException,
            gentl.AccessDeniedException,
            gentl.BusyException,
        )
        with _producers_lock:
            usable = _Usable()
            for path in producers:
                listed = usable.list(genicam, path)
                if listed is None:
                    continue
                entry, infos = listed
                error: DeviceError | None = None
                try:
                    info = next((i for i in infos if i.serial_number == serial), None)
                    if info is None:
                        _release(entry)
                        continue
                    if serial in entry.open_serials:
                        raise DeviceBusy(f"{info.id_} is already open in this process")
                    device = _translate(genicam, info.create_device)
                    flags = gentl.DEVICE_ACCESS_FLAGS_LIST.DEVICE_ACCESS_EXCLUSIVE
                    try:
                        device.open(flags)
                    except busy as exc:
                        error = DeviceBusy(
                            f"{info.id_} is held by another program: {exc}"
                        )
                    except gentl.GenericException as exc:
                        error = DeviceError(f"{info.id_} won't open: {exc}")
                except BaseException:
                    _release(entry)
                    raise
                if error is not None:
                    _release(entry)
                    raise error
                # Outside the try: a connection that fails closes itself, which
                # releases the producer, so releasing here too would be twice.
                conn = _GenTLConnection(genicam, entry, device, serial)
                entry.open_serials.add(serial)
                return conn
            usable.check()
        return None


class _Usable:
    """Loads producers and lists their cameras, skipping any that fail, as IDS
    peak's proxy producer did beside its USB3 Vision one (usb3-vision.md)."""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.listed = 0

    def list(self, genicam: Any, path: str) -> tuple[_Producer, list[Any]] | None:
        """The producer at ``path`` and its cameras, or None if it fails."""
        try:
            entry = _acquire(genicam, path)
        except ImportError as exc:
            self._skip(path, exc)
            return None
        try:
            infos = entry.devices()
        except DeviceError as exc:
            self._skip(path, exc)
            _release_quietly(entry)
            return None
        self.listed += 1
        return entry, infos

    def _skip(self, path: str, exc: Exception) -> None:
        logger.warning("skipping GenTL producer %s: %s", path, exc)
        self.problems.append(f"{path}: {exc}")

    def check(self) -> None:
        """Raise `ImportError` if every producer failed."""
        if not self.listed and self.problems:
            raise ImportError(
                f"No GenTL producer would load ({'; '.join(self.problems)}). "
                f"{GENTL_INSTALL_HINT}"
            )


def _release_quietly(entry: _Producer) -> None:
    """Release ``entry``, logging rather than raising if unloading it fails."""
    try:
        _release(entry)
    except DeviceError as exc:
        logger.warning("%s", exc)


def _node_map(genicam: Any, port: Any, keep: list[Any]) -> Any | None:
    """Load a module's GenICam XML from its port and connect it, or None if the
    port describes no XML. Appends the port adapter to ``keep``."""
    urls = port.url_info_list
    if not urls:
        return None
    url = str(urls[0].url)
    scheme, rest = url.split(":", 1)
    if scheme.lower() == "local":
        # local:///<file name>;<hex address>;<hex size>[?SchemaVersion=...]
        file_name, address, size = rest.split(";")
        file_name = file_name.lstrip("/")
        size = size.split("?")[0]
        read = functools.partial(port.read, int(address, 16), int(size, 16))
        data = bytes(_translate(genicam, read)[1])
    elif scheme.lower() == "file":
        path = unquote(urlparse(url).path)
        if os.name == "nt":
            path = path.lstrip("/")
        file_name = path
        data = Path(path).read_bytes()
    else:
        raise DeviceError(f"can't fetch the camera's description from {url}")
    if file_name.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            member = next((n for n in names if n.lower().endswith(".xml")), None)
            if member is None:
                raise DeviceError(f"the camera's description ({file_name}) has no XML")
            data = archive.read(member)
    else:
        # Ports pad the XML with NULs to the register's size.
        data = data.split(b"\0", 1)[0]
    genapi = genicam.genapi
    node_map = genapi.NodeMap()
    _translate(genicam, lambda: node_map.load_xml_from_string(data.decode("utf-8")))
    adapter = _port_adapter(genapi, port)
    keep.append(adapter)
    _translate(genicam, lambda: node_map.connect(adapter, port.name))
    return node_map


def _port_adapter(genapi: Any, port: Any) -> Any:
    """A ``genapi.AbstractPort`` that reads and writes through a GenTL port."""

    class _Port(genapi.AbstractPort):  # type: ignore[name-defined,misc]
        def read(self, address: int, length: int) -> bytes:
            return port.read(address, length)[1]

        def write(self, address: int, value: bytes) -> None:
            port.write(address, value)

        def get_access_mode(self) -> Any:
            return genapi.EAccessMode.RW

    return _Port()


class _Features:
    """The node-map half of a `Connection`, over a connected ``genapi.NodeMap``."""

    def __init__(self, genicam: Any, nodes: Any) -> None:
        self._genicam = genicam
        self._nodes = nodes

    def _do(self, fn: Callable[[], T]) -> T:
        return _translate(self._genicam, fn)

    def _node(self, name: str) -> Any:
        if not self._nodes.has_node(name):
            raise DeviceError(f"the camera has no {name} node")
        return self._do(lambda: self._nodes.get_node(name))

    def has(self, name: str) -> bool:
        return bool(self._nodes.has_node(name))

    def _access(self, name: str) -> Any:
        if not self.has(name):
            return None
        return self._do(lambda: self._nodes.get_node(name).get_access_mode())

    def is_readable(self, name: str) -> bool:
        modes = self._genicam.genapi.EAccessMode
        return self._access(name) in (modes.RO, modes.RW)

    def is_writable(self, name: str) -> bool:
        modes = self._genicam.genapi.EAccessMode
        return self._access(name) in (modes.WO, modes.RW)

    def _get(self, name: str) -> Any:
        node = self._node(name)
        return self._do(lambda: node.value)

    def _set(self, name: str, value: Any) -> None:
        node = self._node(name)
        self._do(lambda: setattr(node, "value", value))

    def get_float(self, name: str) -> float:
        return float(self._get(name))

    def get_int(self, name: str) -> int:
        return int(self._get(name))

    def get_string(self, name: str) -> str:
        return str(self._get(name))

    def get_enum(self, name: str) -> str:
        return str(self._get(name))

    def float_range(self, name: str) -> tuple[float, float]:
        node = self._node(name)
        return self._do(lambda: (float(node.min), float(node.max)))

    def int_range(self, name: str) -> tuple[int, int, int]:
        node = self._node(name)
        return self._do(lambda: (int(node.min), int(node.max), int(node.inc)))

    def enum_entries(self, name: str) -> tuple[str, ...]:
        # genapi's symbolics are the entries currently available (assumed;
        # usb3-vision.md).
        node = self._node(name)
        return tuple(str(s) for s in self._do(lambda: node.symbolics))

    def set_float(self, name: str, value: float) -> None:
        self._set(name, float(value))

    def set_int(self, name: str, value: int) -> None:
        self._set(name, int(value))

    def set_enum(self, name: str, symbol: str) -> None:
        self._set(name, symbol)

    def execute(self, name: str) -> None:
        node = self._node(name)
        self._do(node.execute)


class _GenTLConnection(_Features):
    """A `Connection` onto one open GenTL device and its first data stream."""

    times_arrivals = False

    def __init__(
        self, genicam: Any, producer: _Producer, device: Any, serial: str
    ) -> None:
        self._serial = serial
        self._genicam = genicam
        self._gentl = genicam.gentl
        self._producer: _Producer | None = producer
        self._device: Any = device
        self._stream: Any = None
        self._ports: list[Any] = []
        self._buffers: list[Any] = []
        self._acquiring = False
        try:
            nodes = _node_map(genicam, device.remote_port, self._ports)
            if nodes is None:
                raise DeviceError("the camera describes no features")
            super().__init__(genicam, nodes)
            stream = self._do(device.create_data_stream)
            self._do(lambda: stream.open(device.data_stream_ids[0]))
            self._stream = stream
            stream_nodes = _node_map(genicam, stream.port, self._ports)
            mode = "StreamBufferHandlingMode"
            if stream_nodes is not None and stream_nodes.has_node(mode):
                # Grabs drain older frames anyway; this saves the producer the copy.
                self._do(
                    lambda: setattr(stream_nodes.get_node(mode), "value", "NewestOnly")
                )
            new_buffer = self._gentl.EVENT_TYPE_LIST.EVENT_NEW_BUFFER
            self._events = self._gentl.EventManagerNewBuffer(
                self._do(lambda: stream.register_event(new_buffer))
            )
        except BaseException:
            try:
                self.close()
            except DeviceError as exc:
                logger.warning("while giving up on a GenTL camera: %s", exc)
            raise

    # --- Stream -----------------------------------------------------------------

    def start_acquisition(self) -> None:
        gentl, stream = self._gentl, self._stream
        payload = self._do(
            lambda: (
                stream.payload_size
                if stream.defines_payload_size()
                else self._get("PayloadSize")
            )
        )
        count = max(BUFFER_COUNT, int(self._do(lambda: stream.buffer_announce_min)))
        # Set first, so a failure part-way still revokes the buffers and unlocks.
        self._acquiring = True
        try:
            for i in range(count):
                # Producers refuse a bytearray here (FLIR's, verified); like
                # Harvesters, hand over bytes, which the producer then fills.
                token = gentl.BufferToken(bytes(payload), i)
                buffer = self._do(functools.partial(stream.announce_buffer, token))
                self._buffers.append(buffer)
                self._do(functools.partial(stream.queue_buffer, buffer))
            # Locks the settings that change the payload while streaming (SFNC).
            if self.is_writable("TLParamsLocked"):
                self.set_int("TLParamsLocked", 1)
            start = gentl.ACQ_START_FLAGS_LIST.ACQ_START_FLAGS_DEFAULT
            self._do(lambda: stream.start_acquisition(start, -1))
            self.execute("AcquisitionStart")
        except BaseException:
            try:
                self.stop_acquisition()
            except DeviceError as exc:
                logger.debug("while giving up on starting acquisition: %s", exc)
            raise

    def stop_acquisition(self) -> None:
        if not self._acquiring:
            return
        self._acquiring = False
        gentl, stream = self._gentl, self._stream
        problems = []
        kill = gentl.ACQ_STOP_FLAGS_LIST.ACQ_STOP_FLAGS_KILL
        discard = gentl.ACQ_QUEUE_TYPE_LIST.ACQ_QUEUE_ALL_DISCARD
        steps: list[tuple[str, Callable[[], Any]]] = [
            ("stop the camera", lambda: self.execute("AcquisitionStop")),
            (
                "stop the stream",
                lambda: self._do(lambda: stream.stop_acquisition(kill)),
            ),
            (
                "flush buffers",
                lambda: self._do(lambda: stream.flush_buffer_queue(discard)),
            ),
            ("flush events", lambda: self._do(self._events.flush_event_queue)),
        ]
        for action, step in steps:
            try:
                step()
            except DeviceError as exc:
                problems.append(f"{action}: {exc}")
        buffers, self._buffers = self._buffers, []
        for buffer in buffers:
            try:
                self._do(functools.partial(stream.revoke_buffer, buffer))
            except DeviceError as exc:
                problems.append(f"revoke a buffer: {exc}")
        try:
            if self.is_writable("TLParamsLocked"):
                self.set_int("TLParamsLocked", 0)
        except DeviceError as exc:
            problems.append(f"unlock TLParamsLocked: {exc}")
        if problems:
            raise DeviceError("; ".join(problems))

    def _wait_once(self, timeout_ms: int) -> Any:
        events = self._events
        try:
            events.update_event_data(timeout_ms)
        except self._gentl.TimeoutException:
            error: DeviceError = DeviceTimeout(f"no frame in {timeout_ms} ms")
        except _gentl_errors(self._genicam) as exc:
            error = DeviceError(str(exc))
        else:
            return events.buffer
        raise error

    def _wait(self, timeout: float | None) -> Any:
        if timeout is not None and timeout <= 0:
            return self._wait_once(0)
        left_ms = math.inf if timeout is None else math.ceil(timeout * 1000)
        while left_ms > 0:
            slice_ms = int(min(_WAIT_SLICE_MS, left_ms))
            try:
                return self._wait_once(slice_ms)
            except DeviceTimeout:
                left_ms -= slice_ms
        raise DeviceTimeout(f"no frame in {timeout:g} s")

    def grab(self, timeout: float | None) -> "_GenTLBuffer":
        if not self._acquiring:
            raise DeviceError("the camera isn't acquiring")
        buffer = self._wait(timeout)
        # Newest only: requeue every older frame already waiting.
        while True:
            try:
                newer = self._wait_once(0)
            except DeviceTimeout:
                break
            except DeviceError:
                self._do(functools.partial(self._stream.queue_buffer, buffer))
                raise
            self._do(functools.partial(self._stream.queue_buffer, buffer))
            buffer = newer
        return _GenTLBuffer(self, buffer)

    def close(self) -> None:
        if self._producer is None:
            return
        problems = []
        try:
            self.stop_acquisition()
        except DeviceError as exc:
            problems.append(str(exc))
        for module in (self._stream, self._device):
            if module is None:
                continue
            try:
                self._do(module.close)
            except DeviceError as exc:
                problems.append(f"close: {exc}")
        self._stream = self._device = None
        producer, self._producer = self._producer, None
        producer.open_serials.discard(self._serial)
        try:
            _release(producer)
        except DeviceError as exc:
            problems.append(str(exc))
        if problems:
            raise DeviceError("; ".join(problems))


class _GenTLBuffer:
    """A `Buffer` over one announced GenTL buffer, requeued on release."""

    def __init__(self, conn: _GenTLConnection, buffer: Any) -> None:
        self._conn = conn
        self._stream = conn._stream
        self._buffer = buffer

    def _read(self, fn: Callable[[Any], T]) -> T:
        buffer = self._buffer
        return self._conn._do(lambda: fn(buffer))

    def status(self) -> BufferStatus:
        complete = self._read(lambda b: b.is_complete())
        return BufferStatus.COMPLETE if complete else BufferStatus.INCOMPLETE

    def status_text(self) -> str:
        return self.status().value

    def device_timestamp_ns(self) -> int:
        try:
            return int(self._read(lambda b: b.timestamp_ns))
        except DeviceError:
            # Producers without a nanosecond timestamp give ticks at the device's
            # timestamp frequency (GenTL standard).
            ticks = self._read(lambda b: b.timestamp)
            frequency = self._conn._do(lambda: self._conn._device.timestamp_frequency)
            return round(ticks * 1e9 / frequency)

    def frame_id(self) -> int:
        return int(self._read(lambda b: b.frame_id))

    def host_timestamp_ns(self) -> None:
        return None

    def pixel_format(self) -> str:
        return pfnc_name(int(self._read(lambda b: b.pixel_format)))

    def pixels(self) -> npt.NDArray[Any]:
        fmt = self.pixel_format()
        width, height = self._read(lambda b: (b.width, b.height))
        return shaped_pixels(self._read(lambda b: b.raw_buffer), fmt, width, height)

    def release(self) -> None:
        self._conn._do(lambda: self._stream.queue_buffer(self._buffer))
