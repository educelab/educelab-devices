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
    find_ports,
    only_port,
    port_with_serial_number,
    wait_for,
)
from educelab.devices.device import (
    DeviceEvent,
    DeviceInfo,
    Disconnected,
    synchronized,
)
from educelab.devices.errors import (
    CommandRejected,
    DeviceBusy,
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
# Slowest speed the stage takes, in mm/s. GRBL rounds feeds to whole mm/min,
# and rejects F0.
MIN_SPEED_MM_S = 0.1

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
    `PositionError` until `home()` runs again. Listeners are called on the
    stage's reader thread.

    With ``timeout=None``, a move's limit is twice its predicted time plus
    `MOVE_MARGIN_S`, and homing's is `HOMING_TIMEOUT_S`, unless
    `move_timeout` is set. A move or homing during a `jog()` raises
    `DeviceBusy`; call `stop()` first.

    Args:
        port: Serial port, e.g. ``"COM3"`` or ``"/dev/cu.usbserial-21320"``.
        limits: ``(minimum, maximum)`` in mm for each of ``"x"``, ``"y"`` and
            ``"z"``, in machine coordinates.
        speed: Starting speed for moves and jogs, in mm/s, at least
            `MIN_SPEED_MM_S`. Each axis is also held to its own maximum rate
            in GRBL (``$110``–``$112``).
        serial_number: USB serial number of the controller's port, matched
            exactly but ignoring case. Found again on each `open()`.
        transport: Connection to use instead of opening ``port`` with
            pyserial. For tests.

    With neither ``port`` nor ``serial_number``, `open()` uses the one
    X-Controller attached, failing if there are none or several.

    Raises:
        ValueError: ``limits`` doesn't give each axis a valid range, ``speed``
            is below `MIN_SPEED_MM_S`, or both ``port`` and ``serial_number``
            were given.
    """

    can_home = True
    requires_homing = True
    move_timeout = None

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
        if not speed >= MIN_SPEED_MM_S:
            raise ValueError(
                f"speed must be at least {MIN_SPEED_MM_S} mm/s, not {speed}"
            )
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
        # Guards matching replies to lines: _orphans counts replies still owed
        # to abandoned lines, and _resets counts resets, which cancel them.
        self._reply_lock = threading.Lock()
        self._orphans = 0
        self._resets = 0
        # Reentrant so stop() can check and abort in one step.
        self._write_lock = threading.RLock()
        self._status_cond = threading.Condition()
        self._status: _Status | None = None
        self._status_seq = 0
        # Reports up to this one may predate the machine's latest change.
        self._status_floor = 0
        self._settings: dict[int, float] = {}
        self._alarms = 0
        self._last_alarm = ""
        # Counts alarms, resets and lost connections, each of which loses
        # homing; homing holds only while this is what it was when it began.
        self._epoch = 0
        self._homing_epoch = 0
        self._homed_epoch: int | None = None
        self._lost: str | None = None
        self._homing = False
        self._jogging = False
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
            for port in find_ports(XCONTROLLERS)
        ]

    @property
    def axes(self) -> tuple[Axis, ...]:
        return self._axes

    def __repr__(self) -> str:
        where = self._port or self._serial_number or "(not found yet)"
        return f"<XCarve {where}>"

    # Connection

    def _open(self) -> None:
        self._forget_link()
        if self._find:
            port = self._find_port()
            self._transport = _default_transport(port.device)
            self._port = port.device
            self._serial_number = port.serial_number
        transport = self._require_transport()
        transport.open()
        try:
            version = wait_for(transport, _parse_banner, BANNER_TIMEOUT_S)
            if version is None:
                # The board didn't reset when the port opened; make it.
                transport.write_bytes(SOFT_RESET)
                version = wait_for(transport, _parse_banner, BANNER_TIMEOUT_S)
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
        reader, self._reader = self._reader, None
        # A listener may close the stage from the reader thread itself.
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2 * READ_POLL_S + 1.0)
        self._require_transport().close()

    def _reset_state(self) -> None:
        super()._reset_state()
        self._forget_link()

    def _forget_link(self) -> None:
        """Forget what the last connection left behind, e.g. owed replies."""
        self._lost = None
        self._status = None
        self._homing = False
        self._jogging = False
        self._expect_reset = False
        self._settings = {}
        self._stop_requested.clear()
        with self._reply_lock:
            self._orphans = 0
            _drain(self._replies)

    def _find_port(self) -> PortInfo:
        if self._serial_number is not None:
            return port_with_serial_number(self._serial_number)
        return only_port(
            XCONTROLLERS,
            name="X-Controller",
            plural="X-Controllers",
            hint="Pass port= or serial_number= for a controller that doesn't "
            "identify itself as one.",
        )

    # Homing

    @property  # type: ignore[override]
    def _homed(self) -> bool:
        return self._homed_epoch is not None and self._homed_epoch == self._epoch

    @_homed.setter
    def _homed(self, homed: bool) -> None:
        # Stage sets this after _home() returns, by when an alarm may have
        # come; the epoch homing began in makes that alarm count.
        self._homed_epoch = self._homing_epoch if homed else None

    def _home(self, timeout: float | None) -> None:
        self._idle_status()
        self._homing_epoch = self._epoch
        self._homing = True
        try:
            self._command("$H", timeout or HOMING_TIMEOUT_S, aborting=SOFT_RESET)
        finally:
            self._homing = False
        self._query_status()
        if self._epoch != self._homing_epoch:
            raise DeviceError(
                f"{self!r} lost homing as it finished: "
                f"{self._last_alarm or 'the controller reset'}"
            )

    # Motion

    def _move_to(self, targets: Mapping[str, float], timeout: float | None) -> None:
        start = self._idle_status().position
        goal = {**start, **targets}
        if timeout is None:
            timeout = self._move_limit(start, goal)
        alarms = self._alarms
        self._command(self._jog_line(targets), aborting=JOG_CANCEL)
        self._await_arrival(goal, timeout, alarms)

    def _jog_line(self, targets: Mapping[str, float]) -> str:
        words = " ".join(f"{name.upper()}{targets[name]:.3f}" for name in targets)
        return f"$J=G53 G21 {words} F{self._speed_mm_s * 60:.0f}"

    def _move_limit(
        self, start: Mapping[str, float], goal: Mapping[str, float]
    ) -> float:
        """Default time limit for a move: twice the prediction, plus a margin."""
        predicted = _predicted_s(start, goal, self._speed_mm_s, self._max_rates)
        return 2 * predicted + MOVE_MARGIN_S

    def _await_arrival(
        self, goal: Mapping[str, float], timeout: float, alarms: int
    ) -> None:
        """Poll status until GRBL is Idle at ``goal``.

        Args:
            alarms: The alarm count from before the jog was sent.

        Raises:
            PositionError: The stage stopped short, or `stop()` was called.
            DeviceError: GRBL raised an alarm.
            DeviceTimeout: It didn't arrive in time.

        The jog is cancelled however this fails.
        """
        started = time.monotonic()
        deadline = started + timeout
        try:
            while True:
                if self._stop_requested.is_set():
                    raise PositionError(f"stop() interrupted the move of {self!r}")
                status = self._query_status()
                if self._alarms != alarms or status.state == "Alarm":
                    raise DeviceError(
                        f"{self!r} raised {self._last_alarm or 'an alarm'} while moving"
                    )
                if status.state == "Idle":
                    if _arrived(status.position, goal):
                        return
                    if time.monotonic() - started > START_GRACE_S:
                        raise PositionError(
                            f"{self!r} stopped short at {_format(status.position)}, "
                            f"moving to {_format(goal)}"
                        )
                if time.monotonic() >= deadline:
                    raise DeviceTimeout(
                        f"{self!r} didn't reach {_format(goal)} in {timeout:g} s; "
                        "the move was cancelled"
                    )
                self._stop_requested.wait(STATUS_INTERVAL_S)
        except BaseException:
            self._abort(JOG_CANCEL)
            raise

    def _jog(self, axis: str, direction: int) -> None:
        # GRBL jogs are finite, so a continuous jog is one to the axis's limit.
        self._idle_status()
        limits = self._axis(axis)
        target = limits.maximum if direction > 0 else limits.minimum
        assert target is not None
        self._command(self._jog_line({axis: target}), aborting=JOG_CANCEL)
        self._jogging = True

    def _stop(self) -> None:
        # Under the write lock, so a line is either sent before the abort or
        # not at all (see _command).
        with self._write_lock:
            self._stop_requested.set()
            self._jogging = False
            if self._lost is not None:
                return
            # Homing ignores jog-cancel; only a reset stops it, and loses homing.
            self._abort(SOFT_RESET if self._homing else JOG_CANCEL)

    def _motion_starting(self) -> None:
        self._stop_requested.clear()
        self._check_link()

    def _idle_status(self) -> _Status:
        """The status once any cancelled jog has finished decelerating.

        GRBL accepts a jog sent while a cancelled one decelerates, then drops
        it when the cancel completes.

        Raises:
            DeviceBusy: A `jog()` is still running, or the stage won't stop.
        """
        status = self._fresh_status()
        if status.state == "Jog":
            if self._jogging:
                raise DeviceBusy(f"{self!r} is jogging; call stop() first")
            status = self._settle()
        if status.state == "Jog":
            raise DeviceBusy(f"{self!r} is still moving after a cancel")
        self._jogging = False
        return status

    def _settle(self, timeout: float = REPLY_TIMEOUT_S) -> _Status:
        """Wait for a cancelled jog to stop, returning the last status."""
        deadline = time.monotonic() + timeout
        while True:
            status = self._query_status()
            if status.state != "Jog" or time.monotonic() >= deadline:
                return status
            time.sleep(STATUS_INTERVAL_S / 2)

    def _position(self) -> Mapping[str, float]:
        return dict(self._fresh_status().position)

    @synchronized
    def max_rates(self) -> dict[str, float]:
        """Each axis's maximum rate in mm/s, as GRBL reported it on open.

        These are ``$110``–``$112``. GRBL holds each axis to its own rate,
        slowing a combined move to match, whatever the stage's speed.

        Raises:
            NotConnected: The stage isn't open.
        """
        return dict(self._max_rates)

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
            aborting: Real-time command that stops what the line starts. It's
                sent if `stop()` is called, the reply doesn't come in time, or
                anything else (e.g. Ctrl-C) interrupts the wait; a line that
                comes after `stop()` isn't sent at all.

        Raises:
            CommandRejected: GRBL answered ``error:N``.
            DeviceError: GRBL raised an alarm or reset while waiting.
            PositionError: `stop()` interrupted it.
            DeviceTimeout: No reply in time.
        """
        alarms = self._alarms
        resets = self._resets
        with self._write_lock:
            if aborting is not None and self._stop_requested.is_set():
                raise PositionError(f"stop() interrupted {line!r} on {self!r}")
            self._require_transport().write_line(line)
        deadline = time.monotonic() + timeout
        answered = False
        abort = aborting is not None
        try:
            while True:
                self._check_link()
                # Stopping homing resets GRBL, which forgets the line, so no
                # reply will come.
                if aborting == SOFT_RESET and self._stop_requested.is_set():
                    abort = False
                    raise PositionError(f"stop() interrupted {line!r} on {self!r}")
                if self._alarms != alarms:
                    abort = False
                    raise DeviceError(
                        f"{self!r} raised {self._last_alarm} during {line!r}"
                    )
                if self._resets != resets:
                    abort = False
                    raise DeviceError(f"{self!r} reset during {line!r}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeviceTimeout(
                        f"{self!r} didn't finish {line!r} in {timeout:g} s"
                    )
                try:
                    reply = self._replies.get(timeout=min(remaining, READ_POLL_S))
                except queue.Empty:
                    continue
                answered = True
                self._invalidate_status()
                # GRBL answers $H "ok" even when homing ends in an alarm.
                if self._alarms != alarms:
                    abort = False
                    raise DeviceError(
                        f"{self!r} raised {self._last_alarm} during {line!r}"
                    )
                if reply != "ok":
                    abort = False
                    code = int(reply.partition(":")[2] or 0)
                    meaning = ERRORS.get(code, "see GRBL's error codes")
                    raise CommandRejected(
                        f"{self!r} rejected {line!r}: error:{code} ({meaning})"
                    )
                # GRBL ignores a jog-cancel that comes before it has read the
                # jog, so a stop() since sending is repeated now that it has.
                if abort and self._stop_requested.is_set():
                    raise PositionError(f"stop() interrupted {line!r} on {self!r}")
                return
        except BaseException:
            if not answered:
                # Before any abort, since a reset makes GRBL forget the line.
                self._abandon(resets)
            if abort:
                assert aborting is not None
                self._abort(aborting)
            raise

    def _abandon(self, resets: int) -> None:
        """Account for the reply to a line no one is waiting for any more.

        GRBL answers every line once, in order, so the reply either came
        already or the next one to come is it. A reset since the line was sent
        means GRBL forgot it and no reply is owed.
        """
        with self._reply_lock:
            if self._resets != resets:
                return
            try:
                late = self._replies.get_nowait()
                logger.debug("%r dropping reply %r to an abandoned line", self, late)
            except queue.Empty:
                self._orphans += 1

    def _abort(self, realtime: bytes) -> None:
        if realtime == SOFT_RESET:
            self._expect_reset = True
        try:
            self._write(realtime)
        except NotConnected:
            pass
        self._invalidate_status()

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

    def _fresh_status(self) -> _Status:
        """The latest status if it's recent and current, or else a new one."""
        with self._status_cond:
            status = self._status
            fresh = (
                status is not None
                and self._status_seq > self._status_floor
                and time.monotonic() - status.at <= STATUS_INTERVAL_S
            )
        return status if fresh and status is not None else self._query_status()

    def _invalidate_status(self) -> None:
        """Mark reports so far as possibly out of date, e.g. after a reply."""
        with self._status_cond:
            self._status_floor = self._status_seq

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
            with self._reply_lock:
                if self._orphans:
                    self._orphans -= 1
                    logger.debug(
                        "%r dropping reply %r to an abandoned line", self, line
                    )
                else:
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
        self._epoch += 1
        self._invalidate_status()
        logger.error("%r raised %s", self, self._last_alarm)
        self._emit(GrblAlarm(self, code, meaning))

    def _on_reset(self, line: str) -> None:
        # A reset discards every line GRBL hadn't answered.
        with self._reply_lock:
            self._resets += 1
            self._orphans = 0
            _drain(self._replies)
        self._epoch += 1
        self._invalidate_status()
        if self._expect_reset:
            self._expect_reset = False
            return
        logger.warning("%r reset unexpectedly (%s); homing is lost", self, line)

    def _on_lost(self, reason: str) -> None:
        self._lost = reason
        self._epoch += 1
        with self._status_cond:
            self._status_cond.notify_all()
        logger.error("%r lost its connection: %s", self, reason)
        self._emit(Disconnected(self, reason))


def _default_transport(port: str) -> SerialTransport:
    return SerialTransport(port, baudrate=BAUD_RATE, timeout=REPLY_TIMEOUT_S)


def _parse_banner(line: str) -> str | None:
    """GRBL's version, if ``line`` is its banner."""
    banner = _BANNER.match(line)
    return banner[1] if banner else None


def _drain(replies: "queue.Queue[str]") -> None:
    while not replies.empty():
        replies.get_nowait()


def _arrived(position: Mapping[str, float], goal: Mapping[str, float]) -> bool:
    return all(
        abs(position[name] - goal[name]) <= ARRIVAL_TOLERANCE_MM for name in AXIS_NAMES
    )


def _format(position: Mapping[str, float]) -> str:
    return ", ".join(f"{name}={position[name]:g}" for name in AXIS_NAMES)


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
