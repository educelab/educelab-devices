"""The X-Carve gantry, through its X-Controller running GRBL 1.1, over USB serial.

Protocol, settings and timing: docs/hardware/xcarve-grbl.md.
"""

import logging
import math
import queue
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass

from educelab.devices._serial import (
    PortInfo,
    PortMatch,
    SerialTransport,
    list_ports,
)
from educelab.devices.device import DeviceEvent, DeviceInfo, Disconnected
from educelab.devices.errors import (
    CommandRejected,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    NotConnected,
    PositionError,
)
from educelab.devices.settings import Setting
from educelab.devices.stages.base import Axis, AxisKind, Stage

__all__ = ["GrblAlarm", "XCarve"]

logger = logging.getLogger(__name__)

BAUD_RATE = 115200
# The X-Controller resets when its port opens and sends its banner once it has
# booted. See docs/hardware/xcarve-grbl.md#transport.
BANNER_TIMEOUT_S = 5.0
REPLY_TIMEOUT_S = 2.0
# Homing seeks each axis across its whole travel at the homing rate.
HOMING_TIMEOUT_S = 120.0
# GRBL's docs advise polling status at no more than 5 Hz.
STATUS_INTERVAL_S = 0.2
# How often the reader thread checks whether it should stop.
READ_POLL_S = 0.1
# GRBL reports positions to 0.001 mm, but steps are coarser; see
# docs/hardware/xcarve-grbl.md#timing.
ARRIVAL_TOLERANCE_MM = 0.05
# GRBL can still report Idle just after accepting a jog.
START_GRACE_S = 0.5
# A default move limit is twice the predicted time plus this, for acceleration.
MOVE_MARGIN_S = 5.0
# Slowest speed set_speed() takes, in mm/s. GRBL itself only needs it positive.
MIN_SPEED_MM_S = 0.1

# What Stage passes the hooks for timeout=None. The base rejects timeouts <= 0,
# so no caller's value can collide with it.
_DEFAULT_TIMEOUT = -1.0

# Real-time commands, which GRBL acts on as soon as it reads them.
STATUS_QUERY = b"?"
JOG_CANCEL = b"\x85"
SOFT_RESET = b"\x18"

# Controllers are found from their USB descriptors alone, since opening a port
# resets the board behind it. Verified on one X-Controller, 2026-10-29.
XCONTROLLERS = PortMatch(vid=0x0403, pid=0x6001, serial_number="XCONTROLLER")

AXIS_NAMES = ("x", "y", "z")

# From GRBL 1.1's interface docs (doc/markdown/interface.md).
ALARMS = {
    1: "hard limit triggered",
    2: "motion target exceeds machine travel",
    3: "reset while in motion",
    4: "probe fail",
    5: "probe fail",
    6: "homing fail: reset during homing",
    7: "homing fail: safety door opened",
    8: "homing fail: pull-off failed to clear the limit switch",
    9: "homing fail: could not find the limit switch",
}
ERRORS = {
    1: "expected command letter",
    2: "bad number format",
    3: "invalid $ statement",
    5: "homing is not enabled",
    8: "not idle",
    9: "locked out during an alarm or jog",
    15: "jog target exceeds machine travel",
    16: "invalid jog command",
    20: "unsupported command",
    22: "feed rate not set",
}

_BANNER = re.compile(r"Grbl (\S+)")


@dataclass(frozen=True, eq=False)
class GrblAlarm(DeviceEvent):
    """GRBL raised an alarm and stopped. Homing is lost until `home()` runs.

    Attributes:
        code: GRBL's alarm number, e.g. 1 for a hard limit.
        meaning: What GRBL's docs say the code means.
    """

    code: int
    meaning: str


@dataclass(frozen=True)
class _Status:
    state: str
    position: dict[str, float]
    at: float


class XCarve(Stage):
    """The X-Carve gantry: axes ``"x"``, ``"y"`` and ``"z"`` in mm.

    Positions are GRBL's machine coordinates, and the limits are the
    consumer's: targets outside them raise `PositionError` and are never
    clamped. The stage must be homed on each connection before it moves, and
    opening never homes. Every move is a GRBL jog, so `stop()` can cancel it;
    `jog()` heads for the axis's limit and stops there if not stopped first.

    An alarm stops the machine and loses homing: listeners get a `GrblAlarm`,
    a move in progress raises `DeviceError`, and later moves raise
    `PositionError` until `home()` runs again.

    Args:
        port: Serial port, e.g. ``"COM3"`` or ``"/dev/cu.usbserial-21320"``.
        limits: ``(minimum, maximum)`` in mm for each of ``"x"``, ``"y"`` and
            ``"z"``, in machine coordinates.
        speed: Starting speed for moves and jogs, in mm/s. Each axis is also
            held to its own maximum rate in GRBL (``$110``–``$112``).
        serial_number: USB serial number of the controller's port, matched
            exactly but ignoring case. Found again on each `open()`.
        transport: Connection to use instead of opening ``port`` with
            pyserial. For tests.

    With neither ``port`` nor ``serial_number``, `open()` uses the one
    X-Controller attached, failing if there are none or several.

    Raises:
        ValueError: ``limits`` doesn't give each axis a valid range, ``speed``
            isn't positive, or both ``port`` and ``serial_number`` were given.
    """

    can_home = True
    requires_homing = True

    def __init__(
        self,
        port: str | None = None,
        *,
        limits: Mapping[str, tuple[float, float]],
        speed: float,
        serial_number: str | None = None,
        transport: SerialTransport | None = None,
    ) -> None:
        if port is not None and serial_number is not None:
            raise ValueError("give an XCarve a port or a serial number, not both")
        if set(limits) != set(AXIS_NAMES):
            raise ValueError(f"limits must give x, y and z, not {sorted(limits)}")
        if not speed > 0:
            raise ValueError(f"speed must be positive, not {speed}")
        super().__init__()
        self._axes = tuple(
            Axis(name, AxisKind.LINEAR, float(limits[name][0]), float(limits[name][1]))
            for name in AXIS_NAMES
        )
        if transport is None and port is not None:
            transport = _default_transport(port)
        self._transport = transport
        self._find = transport is None and port is None
        self._port = None if transport is None else transport.port
        self._serial_number = serial_number
        self._speed_mm_s = float(speed)
        self._firmware_version: str | None = None
        self._max_rates: dict[str, float] = {}

        self._replies: queue.Queue[str] = queue.Queue()
        self._write_lock = threading.Lock()
        self._status_cond = threading.Condition()
        self._status: _Status | None = None
        self._status_seq = 0
        self._settings: dict[int, float] = {}
        self._alarms = 0
        self._last_alarm = ""
        self._lost: str | None = None
        self._homing = False
        self._expect_reset = False
        self._stop_requested = threading.Event()
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="xcarve",
            model="X-Carve",
            address=self._port,
            serial_number=self._serial_number,
            firmware_version=self._firmware_version,
        )

    @classmethod
    def discover(cls) -> list[DeviceInfo]:
        """List the X-Controllers attached, without opening their ports.

        Raises:
            ImportError: pyserial isn't installed.
        """
        return [
            DeviceInfo(
                backend="xcarve",
                model="X-Carve",
                address=port.device,
                serial_number=port.serial_number,
            )
            for port in XCONTROLLERS.matching(list_ports())
        ]

    @property
    def axes(self) -> tuple[Axis, ...]:
        return self._axes

    def __repr__(self) -> str:
        where = self._port or self._serial_number or "(not found yet)"
        return f"<XCarve {where}>"

    # Connection

    def _open(self) -> None:
        if self._find or self._serial_number is not None:
            port = self._find_port()
            self._transport = _default_transport(port.device)
            self._port = port.device
            self._serial_number = port.serial_number
        transport = self._require_transport()
        transport.open()
        try:
            version = _await_banner(transport, BANNER_TIMEOUT_S)
            if version is None:
                # The board didn't reset when the port opened; make it.
                transport.write_bytes(SOFT_RESET)
                version = _await_banner(transport, BANNER_TIMEOUT_S)
            if version is None:
                raise DeviceNotFound(
                    f"no GRBL controller answered on {self._port!r}; check the "
                    "port, or that it isn't held by another program"
                )
            self._firmware_version = version
            self._start_reader(transport)
            self._read_settings()
            if self._speed_mm_s > self._max_speed():
                raise ValueError(
                    f"speed {self._speed_mm_s:g} mm/s is above the controller's "
                    f"fastest axis, {self._max_speed():g} mm/s"
                )
            self._query_status()
        except BaseException:
            self._shutdown()
            raise

    def _close(self) -> None:
        try:
            if self._lost is None:
                self._write(JOG_CANCEL)
        except NotConnected as exc:
            logger.warning("%r could not cancel motion before closing: %s", self, exc)
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        self._reader_stop.set()
        if self._reader is not None:
            self._reader.join(timeout=2 * READ_POLL_S + 1.0)
            self._reader = None
        self._require_transport().close()

    def _reset_state(self) -> None:
        super()._reset_state()
        self._lost = None
        self._status = None
        self._homing = False

    def _find_port(self) -> PortInfo:
        ports = list_ports()
        if self._serial_number is not None:
            wanted = self._serial_number.casefold()
            for port in ports:
                if port.serial_number and port.serial_number.casefold() == wanted:
                    return port
            raise DeviceNotFound(
                f"no serial port has USB serial number {self._serial_number!r}"
            )
        found = XCONTROLLERS.matching(ports)
        if not found:
            raise DeviceNotFound(
                "no X-Controller is attached. Pass port= or serial_number= "
                "for a controller that doesn't identify itself as one."
            )
        if len(found) > 1:
            choices = ", ".join(f"{p.serial_number} at {p.device}" for p in found)
            raise DeviceError(
                f"several X-Controllers are attached ({choices}); "
                "choose one with serial_number="
            )
        return found[0]

    # Homing

    def _home(self, timeout: float) -> None:
        if timeout == _DEFAULT_TIMEOUT:
            timeout = HOMING_TIMEOUT_S
        self._homing = True
        try:
            self._command("$H", timeout, aborting=SOFT_RESET)
        finally:
            self._homing = False
        self._query_status()

    def _timeout(self, timeout: float | None) -> float:
        return _DEFAULT_TIMEOUT if timeout is None else super()._timeout(timeout)

    # Motion: added with blocking moves, jogs and stop.

    def _move_to(self, targets: Mapping[str, float], timeout: float) -> None:
        raise NotImplementedError

    def _jog(self, axis: str, direction: int) -> None:
        raise NotImplementedError

    def _stop(self) -> None:
        raise NotImplementedError

    def _position(self) -> Mapping[str, float]:
        status = self._status
        if status is None or time.monotonic() - status.at > STATUS_INTERVAL_S:
            status = self._query_status()
        return dict(status.position)

    def _speed(self) -> Setting:
        return Setting(
            "speed",
            self._speed_mm_s,
            minimum=MIN_SPEED_MM_S,
            maximum=self._max_speed(),
            unit="mm/s",
        )

    def _set_speed(self, speed: float) -> None:
        self._speed_mm_s = speed

    def _max_speed(self) -> float:
        return max(self._max_rates.values())

    # Talking to GRBL

    def _command(
        self, line: str, timeout: float = REPLY_TIMEOUT_S, aborting: bytes | None = None
    ) -> None:
        """Send a line and wait for its ``ok``.

        Args:
            aborting: Real-time command that stops what the line started, sent
                if the reply doesn't come in time, an alarm interrupts it, or
                `stop()` is called.

        Raises:
            CommandRejected: GRBL answered ``error:N``.
            DeviceError: GRBL raised an alarm while waiting.
            PositionError: `stop()` interrupted it.
            DeviceTimeout: No reply in time.
        """
        # A reply that arrived after an earlier command gave up waiting.
        while not self._replies.empty():
            logger.debug("%r discarding late reply %r", self, self._replies.get())
        alarms = self._alarms
        self._write_line(line)
        deadline = time.monotonic() + timeout
        while True:
            self._check_link()
            if self._alarms != alarms:
                raise DeviceError(f"{self!r} raised {self._last_alarm} during {line!r}")
            if self._stop_requested.is_set():
                raise PositionError(f"stop() interrupted {line!r} on {self!r}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if aborting is not None:
                    self._abort(aborting)
                raise DeviceTimeout(f"{self!r} didn't finish {line!r} in {timeout:g} s")
            try:
                reply = self._replies.get(timeout=min(remaining, READ_POLL_S))
            except queue.Empty:
                continue
            if reply == "ok":
                return
            code = int(reply.partition(":")[2] or 0)
            meaning = ERRORS.get(code, "see GRBL's error codes")
            raise CommandRejected(
                f"{self!r} rejected {line!r}: error:{code} ({meaning})"
            )

    def _abort(self, realtime: bytes) -> None:
        if realtime == SOFT_RESET:
            self._expect_reset = True
        try:
            self._write(realtime)
        except NotConnected:
            pass

    def _read_settings(self) -> None:
        self._command("$$")
        try:
            self._max_rates = {
                name: self._settings[110 + i] / 60 for i, name in enumerate(AXIS_NAMES)
            }
        except KeyError as exc:
            raise DeviceError(
                f"{self!r} didn't report its maximum rates ($110-$112)"
            ) from exc

    def _query_status(self, timeout: float = REPLY_TIMEOUT_S) -> _Status:
        """Ask for a status report and wait for one newer than the request."""
        with self._status_cond:
            seen = self._status_seq
        self._write(STATUS_QUERY)
        deadline = time.monotonic() + timeout
        with self._status_cond:
            while self._status_seq == seen:
                self._check_link()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeviceTimeout(f"{self!r} sent no status report")
                self._status_cond.wait(min(remaining, READ_POLL_S))
            assert self._status is not None
            return self._status

    def _write_line(self, line: str) -> None:
        with self._write_lock:
            self._require_transport().write_line(line)

    def _write(self, realtime: bytes) -> None:
        with self._write_lock:
            self._require_transport().write_bytes(realtime)

    def _check_link(self) -> None:
        if self._lost is not None:
            raise NotConnected(f"{self!r} lost its connection: {self._lost}")

    def _require_transport(self) -> SerialTransport:
        # Only None before a stage without a port first finds its controller.
        assert self._transport is not None, "no transport before open()"
        return self._transport

    # The reader thread

    def _start_reader(self, transport: SerialTransport) -> None:
        self._reader_stop = threading.Event()
        self._reader = threading.Thread(
            target=self._read_loop,
            args=(transport, self._reader_stop),
            name=f"xcarve-reader {self._port}",
            daemon=True,
        )
        self._reader.start()

    def _read_loop(self, transport: SerialTransport, stopping: threading.Event) -> None:
        while not stopping.is_set():
            try:
                line = transport.read_line(READ_POLL_S)
            except DeviceTimeout:
                continue
            except NotConnected as exc:
                if not stopping.is_set():
                    self._on_lost(str(exc))
                return
            try:
                self._dispatch(line)
            except Exception:
                logger.exception("%r couldn't handle %r", self, line)

    def _dispatch(self, line: str) -> None:
        if line == "ok" or line.startswith("error:"):
            self._replies.put(line)
        elif line.startswith("<") and line.endswith(">"):
            self._on_status(line[1:-1])
        elif line.startswith("ALARM:"):
            self._on_alarm(line)
        elif _BANNER.match(line):
            self._on_reset(line)
        elif line.startswith("$") and "=" in line:
            number, _, value = line[1:].partition("=")
            self._settings[int(number)] = float(value)
        else:
            logger.info("%r says %s", self, line)

    def _on_status(self, report: str) -> None:
        state, *fields = report.split("|")
        for field in fields:
            name, _, value = field.partition(":")
            if name == "MPos":
                coords = [float(v) for v in value.split(",")]
                status = _Status(
                    state.partition(":")[0],
                    dict(zip(AXIS_NAMES, coords, strict=False)),
                    time.monotonic(),
                )
                with self._status_cond:
                    self._status = status
                    self._status_seq += 1
                    self._status_cond.notify_all()
                return
        logger.warning("%r status report has no MPos: %r", self, report)

    def _on_alarm(self, line: str) -> None:
        code = int(line.partition(":")[2] or 0)
        meaning = ALARMS.get(code, "see GRBL's alarm codes")
        self._last_alarm = f"{line} ({meaning})"
        self._alarms += 1
        self._homed = False
        logger.error("%r raised %s", self, self._last_alarm)
        self._emit(GrblAlarm(self, code, meaning))

    def _on_reset(self, line: str) -> None:
        self._homed = False
        if self._expect_reset:
            self._expect_reset = False
            return
        logger.warning("%r reset unexpectedly (%s); homing is lost", self, line)

    def _on_lost(self, reason: str) -> None:
        self._lost = reason
        self._homed = False
        with self._status_cond:
            self._status_cond.notify_all()
        logger.error("%r lost its connection: %s", self, reason)
        self._emit(Disconnected(self, reason))


def _default_transport(port: str) -> SerialTransport:
    return SerialTransport(port, baudrate=BAUD_RATE, timeout=REPLY_TIMEOUT_S)


def _await_banner(transport: SerialTransport, timeout: float) -> str | None:
    """Wait for GRBL's banner and return its version, skipping anything else."""
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            line = transport.read_line(remaining)
        except DeviceTimeout:
            return None
        if banner := _BANNER.match(line):
            return banner[1]
    return None


def _predicted_s(
    start: Mapping[str, float], goal: Mapping[str, float], feed: float, rates: dict
) -> float:
    """How long GRBL takes to jog from ``start`` to ``goal``, ignoring acceleration.

    ``feed`` and ``rates`` are in mm/s. GRBL slows the whole move so that no
    axis exceeds its own maximum rate.
    """
    deltas = {name: abs(goal[name] - start[name]) for name in AXIS_NAMES}
    return max(
        math.hypot(*deltas.values()) / feed,
        *(deltas[name] / rates[name] for name in AXIS_NAMES),
    )
