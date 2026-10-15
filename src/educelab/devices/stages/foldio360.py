"""Driver for the Orangemonkie Foldio360 Bluetooth turntable.

The turntable speaks bare ASCII over a Nordic UART Service (NUS). bleak is
async-only, so the transport runs a private asyncio loop on a daemon thread and
every public method blocks on it (ADR 0001).

Everything this module encodes about the hardware is documented, with how sure
we are of it, in ``docs/hardware/foldio360.md``.
"""

import asyncio
import concurrent.futures
import logging
import math
import queue
import re
import threading
import time
from collections.abc import Callable, Coroutine, Mapping
from typing import Any, NoReturn, TypeVar

from educelab.devices.device import DeviceInfo, synchronized
from educelab.devices.errors import (
    CommandRejected,
    DeviceBusy,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    NotConnected,
    PositionError,
    import_optional,
)
from educelab.devices.settings import Setting
from educelab.devices.stages.base import Axis, AxisKind, Stage

__all__ = [
    "CCW",
    "CW",
    "KNOWN_GOOD_FRAMES",
    "Foldio360",
    "NusTransport",
    "UnexpectedResponse",
    "predicted_move_seconds",
    "scan_lines",
    "watchdog_seconds",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Addressed by UUID rather than by handle: handles are stable for firmware
# V1.0.2 but not promised (hardware doc, Transport).
NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # host -> device, commands
NUS_TX_CHAR = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device -> host, responses

DEVICE_NAME = "Foldio360"

# The link never negotiates above the 23-byte ATT default (hardware doc, Framing).
MAX_PAYLOAD = 20

# Rotation timing model (hardware doc, Timing). Speeds 2 and 3 assume speed 1's
# intercept. Used only for watchdogs, never in place of the OK notification.
SLOPE_S_PER_DEG = {1: 0.13696, 2: 0.10303, 3: 0.06812}
INTERCEPT_S = 0.1403

# The speeds the firmware accepts. Kept separate from the timing table: adding a
# timing fit must not quietly widen what validates.
SPEEDS = (1, 2, 3)

# Wire direction values. The official app labels CW "Left" and CCW "Right"
# (hardware doc, rotate).
CW = "CW"
CCW = "CCW"

# rotate/cancel mode: 1 travels a fixed angle and stops, 0 runs until cancelled.
MODE_FIXED = 1
MODE_JOG = 0

# Every command except a move replies within ~200 ms; a generous ceiling for
# those, used wherever the timing model does not apply.
ACK_TIMEOUT_S = 5.0

# The four reply shapes (hardware doc, Response grammar). Anything else is the
# head of a fragmented reply.
_RESPONSE = re.compile(r"^(?:OK|FAIL|\d+|V\d+\.\d+\.\d+_\S+)$")

# Replies have no terminator, so a fragment can only be waited for. Observed
# notifications all arrived whole within ~500 ms of the write.
_FRAGMENT_GRACE_S = 0.25

# A complete `3` and the head of `302` are the same bytes, so a digits-only
# reply gets a brief settle before it is believed.
_NUMERIC_SETTLE_S = 0.030

# Sustained rate of each firmware speed, the reciprocal of its timing slope.
# Speeds 2 and 3 are assumed (hardware doc, Timing).
_DEG_PER_S = {level: 1 / SLOPE_S_PER_DEG[level] for level in SPEEDS}

_THETA = "theta"
_AXES = (Axis(_THETA, AxisKind.ROTARY, wraps=True),)

# The counter counts whole degrees and is good to ±1° (hardware doc,
# get_position / get_bright), so that is how close a move must land.
_ARRIVAL_TOLERANCE_DEG = 1

# Rotations up to 380° are verified; longer ones are untried (hardware doc,
# Open questions), so longer turns are split into legs of one revolution.
_MAX_LEG_DEG = 360

# How often a move waiting for its OK checks for stop().
_POLL_S = 0.05

# After a cancel or a fire-and-forget write, how long the link must be silent
# before stray replies count as drained. Replies come within ~200 ms.
_SETTLE_QUIET_S = 0.3

# Frame counts run() is known to complete; others wedge after frame 1 (hardware
# doc, run / continue / ir_shutter). Passed through, not enforced.
KNOWN_GOOD_FRAMES = (24, 36, 48)

# Two cancels stop motion of an unknown mode; they must be spaced past the
# ~100 ms window in which back-to-back commands cost an ack (hardware doc, rotate).
_CANCEL_SPACING_S = 0.15

# On macOS a missing Bluetooth permission and a switched-off turntable look the
# same: an empty scan with no error. Worth saying every time.
EMPTY_SCAN_NOTE = (
    "No BLE devices at all in {seconds:.0f}s. On macOS that is what a missing "
    "Bluetooth permission looks like: System Settings > Privacy & Security > "
    "Bluetooth."
)


class UnexpectedResponse(DeviceError):
    """The turntable sent a reply that makes no sense for the command.

    Unlike `CommandRejected` (the device answered FAIL), this almost always
    means the driver has lost command/response sync.
    """


def predicted_move_seconds(degrees: float, speed: int) -> float:
    """Predicted duration of a rotation, in seconds, from the timing model."""
    if speed not in SLOPE_S_PER_DEG:
        raise ValueError(f"speed must be 1, 2 or 3, got {speed!r}")
    return SLOPE_S_PER_DEG[speed] * abs(degrees) + INTERCEPT_S


def watchdog_seconds(degrees: float, speed: int) -> float:
    """Timeout for a rotation: double the prediction, plus a second.

    Deliberately loose. Speeds 2 and 3 rest on an assumed intercept, and a
    false abort on a healthy move is worse than a late one on a wedged move.
    """
    return 2 * predicted_move_seconds(degrees, speed) + 1.0


def _bleak() -> Any:
    return import_optional("bleak", extra="foldio360")


class _LoopThread:
    """A private asyncio loop on a daemon thread, so callers stay synchronous."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="foldio360-ble", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run a coroutine on the loop thread and block for its result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise DeviceTimeout(f"no reply within {timeout:.1f}s") from None

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        if not self._thread.is_alive():
            self._loop.close()


def _is_foldio(device: Any, adv: Any, name: str = DEVICE_NAME) -> bool:
    """Whether an advertisement is the turntable, by service UUID or by name.

    Firmware V1.0.2 advertises both; matching either survives a firmware that
    drops one.
    """
    uuids = {u.lower() for u in (adv.service_uuids or ())}
    return NUS_SERVICE in uuids or (adv.local_name or device.name) == name


def scan_lines(scan_timeout: float = 8.0) -> list[str]:
    """Scan and describe every advertising BLE device, strongest first.

    Returns one line per device, with the turntable marked. An empty list means
    nothing advertised at all (see `EMPTY_SCAN_NOTE`).
    """
    scanner = _bleak().BleakScanner
    found = asyncio.run(scanner.discover(timeout=scan_timeout, return_adv=True))
    lines = []
    for device, adv in sorted(found.values(), key=lambda p: -(p[1].rssi or -999)):
        name = adv.local_name or device.name or "-"
        mark = "  <-- Foldio360" if _is_foldio(device, adv) else ""
        # Some advertisers report no RSSI at all.
        rssi = "   -" if adv.rssi is None else f"{adv.rssi:>4}"
        lines.append(f"{device.address}  {rssi} dBm  {name}{mark}")
    return lines


class NusTransport:
    """Byte pipe to the turntable's Nordic UART Service.

    Moves ASCII commands one way and queues notifications coming back. It knows
    the reply grammar only well enough to reassemble fragments; command logic
    lives in `Foldio360`, which tests drive with a scripted fake instead.

    Args:
        address: BLE address to connect to, or ``None`` to scan for one.
        name: Advertised name to match when scanning.
        scan_timeout: Seconds to scan for.
        connect_timeout: Seconds to wait for a connection, or ``None`` for
            bleak's default (30 s). Worth lowering when the address is a guess.
    """

    def __init__(
        self,
        address: str | None = None,
        name: str = DEVICE_NAME,
        scan_timeout: float = 8.0,
        connect_timeout: float | None = None,
    ) -> None:
        self.address = address
        self.name = name
        self.scan_timeout = scan_timeout
        self.connect_timeout = connect_timeout
        self._client: Any = None
        self._loop: _LoopThread | None = None
        # Notification chunks, not replies: the wire has no delimiter, so the
        # reader decides the framing.
        self._chunks: queue.Queue[bytes] = queue.Queue()

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def connect(self) -> None:
        """Scan if needed, connect, and enable notifications. Idempotent.

        Notifications are enabled before this returns, so no reply is dropped.
        """
        if self.connected:
            return
        # A dropped link leaves a dead client and a live loop thread behind;
        # tear them down so this session doesn't inherit stale notifications.
        self.close()
        self.drain()
        self._loop = _LoopThread()
        try:
            self._loop.call(self._connect())
        except Exception:
            self._loop.stop()
            self._loop = None
            raise

    async def _connect(self) -> None:
        bleak = _bleak()
        target = self.address or await self._discover()
        extra = (
            {} if self.connect_timeout is None else {"timeout": self.connect_timeout}
        )
        client = bleak.BleakClient(target, **extra)
        await client.connect()
        try:
            await client.start_notify(NUS_TX_CHAR, self._on_notify)
        except Exception:
            # Only one central may hold the link, so don't leave it held.
            await client.disconnect()
            raise
        self._client = client
        self.address = getattr(client, "address", target)

    async def _discover(self) -> Any:
        found = await _bleak().BleakScanner.discover(
            timeout=self.scan_timeout, return_adv=True
        )
        for device, adv in found.values():
            if _is_foldio(device, adv, self.name):
                return device
        raise DeviceNotFound(
            f"no {self.name!r} found in {self.scan_timeout:.0f}s "
            f"({len(found)} device(s) seen). Check the turntable is powered on, "
            "that the official app is not holding the link, and on macOS that "
            "this terminal has Bluetooth permission in System Settings > "
            "Privacy & Security; without it scans return empty, with no error."
        )

    def _on_notify(self, _char: Any, data: bytearray) -> None:
        """bleak notification callback. Runs on the loop thread."""
        logger.debug("<- %r", bytes(data))
        self._chunks.put(bytes(data))

    def close(self) -> None:
        """Disconnect and stop the loop thread. Idempotent and never raises.

        Callers close from ``finally`` blocks, where a teardown error would hide
        the real one; switching the turntable off mid-command makes that common.
        """
        if self._loop is None:
            self._client = None
            return
        try:
            if self._client is not None:
                self._loop.call(self._disconnect(), timeout=10.0)
        except Exception:
            # Usually the link is already gone; nothing more to release.
            logger.debug("Foldio360 disconnect failed", exc_info=True)
        finally:
            self._client = None
            self._loop.stop()
            self._loop = None

    async def _disconnect(self) -> None:
        try:
            await self._client.stop_notify(NUS_TX_CHAR)
        except Exception:
            # Nothing to unsubscribe from if the link is already gone.
            logger.debug("Foldio360 stop_notify failed", exc_info=True)
        await self._client.disconnect()

    def send(self, command: str | bytes) -> None:
        """Write a command, chunked at the 20-byte payload ceiling.

        Takes bytes for commands that aren't ASCII (``set_signal``). Uses
        unacknowledged writes, matching the official app.
        """
        if not self.connected or self._loop is None:
            raise NotConnected("Foldio360 is not connected; call connect() first")
        logger.debug("-> %r", command)
        payload = command.encode("ascii") if isinstance(command, str) else command
        self._loop.call(self._write(payload), timeout=10.0)

    async def _write(self, payload: bytes) -> None:
        for start in range(0, len(payload), MAX_PAYLOAD):
            chunk = payload[start : start + MAX_PAYLOAD]
            await self._client.write_gatt_char(NUS_RX_CHAR, chunk, response=False)

    def read(self, timeout: float | None) -> str:
        """Block for one reply and return it as text.

        Replies have no terminator, so a chunk that doesn't match the reply
        grammar is treated as a fragment and given a brief window to complete.
        Digits-only replies always get a short settle, since a truncated
        position would otherwise pass as a whole one.

        Raises:
            NotConnected: The link is down.
            DeviceTimeout: Nothing arrived within ``timeout`` seconds.
        """
        if not self.connected:
            raise NotConnected("Foldio360 is not connected; call connect() first")
        text = self._next_chunk(timeout)
        text = self._gather(text, _FRAGMENT_GRACE_S, lambda s: not _RESPONSE.match(s))
        return self._gather(text, _NUMERIC_SETTLE_S, str.isdigit)

    def _gather(
        self, text: str, window: float, incomplete: Callable[[str], object]
    ) -> str:
        """Append chunks while ``incomplete(text)``, for at most ``window`` seconds."""
        deadline = time.monotonic() + window
        while incomplete(text):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break  # return what arrived rather than lose it
            try:
                text += self._next_chunk(remaining)
            except DeviceTimeout:
                break
        return text

    def _next_chunk(self, timeout: float | None) -> str:
        try:
            chunk = self._chunks.get(timeout=timeout)
        except queue.Empty:
            raise DeviceTimeout(f"no notification within {timeout:.1f}s") from None
        return chunk.decode("ascii", "replace").strip()

    def drain(self) -> None:
        """Discard queued notifications.

        With no sequence numbers, a late reply would otherwise be read as the
        next command's (hardware doc, Command/response pairing).
        """
        while True:
            try:
                self._chunks.get_nowait()
            except queue.Empty:
                return


class Foldio360(Stage):
    """The Foldio360 turntable, as a stage with one wrapping rotary axis.

    The axis is ``"theta"``, in degrees, counted clockwise by the turntable's
    own position counter. That counter has no hardware reference: it re-zeros
    wherever the platter sits at power-on, so the stage can't home (ADR 0003).

    Positions are whole degrees. Targets are rounded to the nearest degree,
    and a move counts as arrived when the counter reads within 1° of the
    target; otherwise it raises `PositionError`.

    With ``timeout=None``, a move's limit comes from the predicted move time
    (`watchdog_seconds`) rather than `move_timeout`. Turns longer than one
    revolution are made as several rotations of at most 360°.

    Only one BLE central may hold the link, so close the official app before
    opening. See ``docs/hardware/foldio360.md`` for everything the driver
    relies on.

    Args:
        address: BLE address, or ``None`` to scan for the turntable.
        name: Advertised name to match when scanning.
        scan_timeout: Seconds to scan for.
        connect_timeout: Seconds to wait for a connection, or ``None`` for
            bleak's default.
        transport: Replaces the BLE transport; tests inject a fake.
    """

    def __init__(
        self,
        address: str | None = None,
        *,
        name: str = DEVICE_NAME,
        scan_timeout: float = 8.0,
        connect_timeout: float | None = None,
        transport: Any = None,
    ) -> None:
        super().__init__()
        self._transport = transport or NusTransport(
            address, name, scan_timeout, connect_timeout
        )
        self._firmware: str | None = None
        self._speed_level = 1
        # Guards the state below, which stop() and position() read without
        # the device lock.
        self._state_lock = threading.Lock()
        self._last_position = 0
        self._motion: int | None = None  # mode of the rotate awaiting its reply
        self._running = False  # inside run()
        self._jogging = False
        self._stop_requested = threading.Event()
        # Replies that no command will claim: acks from fire-and-forget writes
        # and from cancels sent while another command was waiting.
        self._strays = 0

    @property
    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="foldio360",
            model="Foldio360",
            address=getattr(self._transport, "address", None),
            firmware_version=self._firmware,
        )

    @property
    def axes(self) -> tuple[Axis, ...]:
        return _AXES

    def __repr__(self) -> str:
        address = getattr(self._transport, "address", None)
        return f"<Foldio360 {address}>" if address else "<Foldio360>"

    # -- lifecycle --------------------------------------------------------------

    def _open(self) -> None:
        self._transport.connect()
        try:
            self._firmware = self._command("get_status")
            self._read_position()
        except BaseException:
            self._transport.close()
            raise

    def _close(self) -> None:
        try:
            if self._jogging:
                self._safe_cancel(MODE_JOG)
        finally:
            self._transport.close()

    def _reset_state(self) -> None:
        super()._reset_state()
        self._firmware = None
        with self._state_lock:
            self._motion = None
            self._jogging = False
            self._running = False
            self._strays = 0
        self._stop_requested.clear()

    # -- stage hooks --------------------------------------------------------------

    def _timeout(self, timeout: float | None) -> float:
        # None reaches the hooks as inf, meaning "derive from the timing model".
        return math.inf if timeout is None else super()._timeout(timeout)

    def _move_to(self, targets: Mapping[str, float], timeout: float) -> None:
        self._check_idle()
        target = round(targets[_THETA]) % 360
        current = self._read_position()
        # Shortest signed path in [-180, 180); an exact half-turn goes CCW.
        delta = (target - current + 540) % 360 - 180
        self._turn(delta, current, timeout)

    def _move_by(
        self, offsets: Mapping[str, float], start: Mapping[str, float], timeout: float
    ) -> None:
        self._check_idle()
        self._turn(round(offsets[_THETA]), round(start[_THETA]) % 360, timeout)

    def _jog(self, axis: str, direction: int) -> None:
        self._check_idle()
        wire = CW if direction > 0 else CCW
        with self._state_lock:
            self._motion = MODE_JOG
            stopped = self._stop_requested.is_set()
        try:
            if stopped:
                return  # a stop() arrived while the jog was starting
            reply = self._command(
                f"rotate({wire},0,{self._speed_level},{MODE_JOG})", expect_ok=True
            )
            _check_ok(reply, "jog")
            with self._state_lock:
                # A stop() during the spin-up ack has already cancelled it.
                self._jogging = not self._stop_requested.is_set()
        except DeviceTimeout:
            self._safe_cancel(MODE_JOG)
            raise
        finally:
            with self._state_lock:
                self._motion = None

    def _motion_starting(self) -> None:
        self._stop_requested.clear()

    def _stop(self) -> None:
        self._stop_requested.set()
        if self._lock.acquire(blocking=False):
            try:
                self._stop_idle()
            finally:
                self._lock.release()
            return
        # Another thread holds the lock, usually waiting on a move: send the
        # cancel without waiting for its ack, which that thread will discard.
        with self._state_lock:
            mode = MODE_JOG if self._jogging else self._motion
            if mode is not None:
                self._strays += 1
        if mode is not None:
            self._transport.send(f"cancel({mode})")

    def _position(self) -> Mapping[str, float]:
        # Ask the turntable when nothing else is talking to it (it follows
        # jogs); during a move, report the last position read.
        if self._lock.acquire(blocking=False):
            try:
                if self._is_open:
                    self._read_position()
            finally:
                self._lock.release()
        with self._state_lock:
            return {_THETA: float(self._last_position)}

    def _speed(self) -> Setting:
        return Setting(
            "speed",
            _DEG_PER_S[self._speed_level],
            choices=tuple(_DEG_PER_S[level] for level in SPEEDS),
            unit="deg/s",
        )

    def _set_speed(self, speed: float) -> None:
        for level, rate in _DEG_PER_S.items():
            if rate == speed:
                self._speed_level = level
                return
        raise ValueError(f"{speed} deg/s is not one of the Foldio360's speeds")

    # -- halo ---------------------------------------------------------------------

    @synchronized
    def get_brightness(self) -> int:
        """Return the halo level, 0-100."""
        return self._expect_int("get_bright", 100)

    @synchronized
    def set_brightness(self, level: int, wait: bool = True) -> None:
        """Set the halo LED ring, 0-100. Takes effect immediately.

        Pass ``wait=False`` when ramping: the device drops acks under rapid
        writes even though the values land.
        """
        if not isinstance(level, int) or not 0 <= level <= 100:
            raise ValueError(f"brightness must be an int in 0..100, got {level!r}")
        command = f"set_bright({level})"
        if wait:
            _check_ok(self._command(command, expect_ok=True), command)
            return
        self._settle()
        self._transport.send(command)
        with self._state_lock:
            self._strays += 1

    # -- capture sequence and IR shutter --------------------------------------------

    @synchronized
    def run(self, frames: int, on_frame: Callable[[int], object]) -> None:
        """Step through a full revolution, calling ``on_frame`` at each frame.

        The turntable steps 360/``frames`` degrees at the stage's speed, then
        waits: ``on_frame(index)`` is called with each 1-based frame index,
        and the next step starts when it returns. Frame 1 is the current
        position. Return ``False`` from ``on_frame`` to end the sequence early;
        if it raises, the sequence is cancelled and the error propagates.

        Only 24, 36, and 48 frames are known to complete (`KNOWN_GOOD_FRAMES`).
        Other counts are passed through, but the firmware wedges after frame
        1, which surfaces as `DeviceTimeout` once the sequence is cancelled.
        Unverified with this driver; see the hardware doc,
        run / continue / ir_shutter.

        Raises:
            ValueError: ``frames`` is not a positive int.
            PositionError: `stop()` interrupted the sequence.
            DeviceTimeout: A step didn't answer in time.
        """
        if isinstance(frames, bool) or not isinstance(frames, int) or frames < 1:
            raise ValueError(f"frames must be an int >= 1, got {frames!r}")
        self._motion_starting()
        self._check_idle()
        level = self._speed_level
        per_frame = max(watchdog_seconds(360.0 / frames, level), ACK_TIMEOUT_S)
        hint = (
            f". run({frames}) wedges unless frames is one of "
            f"{', '.join(map(str, KNOWN_GOOD_FRAMES))}"
        )
        with self._state_lock:
            self._motion = MODE_FIXED
            self._running = True
            stopped = self._stop_requested.is_set()
        try:
            if stopped:
                self._interrupted()
            command = f"run({frames},{level})"
            index = self._step(command, ACK_TIMEOUT_S, hint)
            while index:
                try:
                    keep_going = on_frame(index)
                except BaseException:
                    self._safe_cancel(MODE_FIXED)
                    raise
                if self._stop_requested.is_set():
                    self._interrupted()
                if keep_going is False:
                    self._safe_cancel(MODE_FIXED)
                    return
                index = self._step("continue", per_frame, hint)
        finally:
            with self._state_lock:
                self._motion = None
                self._running = False
            self._refresh_position()

    @synchronized
    def ir_shutter(self, argument: int = 0) -> int:
        """Fire the IR shutter and return the frame index the device echoes.

        Needs a waveform chosen with `set_signal()` first, or the device
        answers FAIL. Inside `run()`, the echo is the current frame index.
        Unverified with this driver; see the hardware doc,
        run / continue / ir_shutter.

        Raises:
            CommandRejected: No waveform is set, or ``argument`` isn't valid
                (only 0 is known to work).
        """
        return self._expect_int(f"ir_shutter({argument})")

    @synchronized
    def set_signal(self, blob: bytes) -> None:
        """Choose the IR shutter's waveform, as a raw timing blob.

        The blob is only partly decoded; capture one for a camera by stepping
        the official app's camera picker. Unverified with this driver; see the
        hardware doc, set_signal.

        Raises:
            TypeError: ``blob`` is not bytes.
        """
        if not isinstance(blob, bytes | bytearray):
            raise TypeError(f"set_signal takes bytes, not {type(blob).__name__}")
        command = b"set_signal(" + bytes(blob) + b")"
        _check_ok(self._command(command, expect_ok=True), _render(command))

    def _step(self, command: str, limit: float, hint: str) -> int:
        """Send one run-sequence command and return the frame index it reports."""
        self._settle()
        self._transport.send(command)
        return self._parse_int(self._await_reply(command, limit, hint), command)

    # -- motion -------------------------------------------------------------------

    def _check_idle(self) -> None:
        # What the firmware does with a rotate during a jog or a run sequence
        # is untested.
        if self._jogging:
            raise DeviceBusy(f"{self!r} is jogging; call stop() first")
        if self._running:
            raise DeviceBusy(f"{self!r} is running a capture sequence")

    def _turn(self, degrees: int, start: int, timeout: float) -> None:
        """Turn by a signed whole-degree offset, in legs of at most 360°."""
        deadline = None if math.isinf(timeout) else time.monotonic() + timeout
        remaining = degrees
        position = start
        while remaining:
            leg = max(-_MAX_LEG_DEG, min(_MAX_LEG_DEG, remaining))
            if deadline is None:
                limit = watchdog_seconds(leg, self._speed_level)
            else:
                limit = deadline - time.monotonic()
                if limit <= 0:
                    raise DeviceTimeout(
                        f"{self!r} move did not finish within {timeout:g} s"
                    )
            position = self._rotate(leg, position, limit)
            remaining -= leg

    def _rotate(self, degrees: int, start: int, limit: float) -> int:
        """Make one fixed-angle rotation and check where it ended.

        Returns the position read afterwards.
        """
        wire = CW if degrees > 0 else CCW
        command = f"rotate({wire},{abs(degrees)},{self._speed_level},{MODE_FIXED})"
        with self._state_lock:
            self._motion = MODE_FIXED
            stopped = self._stop_requested.is_set()
        try:
            if stopped:
                self._interrupted()
            self._settle()
            self._transport.send(command)
            _check_ok(self._await_reply(command, limit), command)
        finally:
            with self._state_lock:
                self._motion = None
        expected = (start + degrees) % 360
        actual = self._read_position()
        if _angle_between(actual, expected) > _ARRIVAL_TOLERANCE_DEG:
            raise PositionError(
                f"{self!r} reads {actual}° after turning to {expected}°. The "
                "counter only tracks commanded motion, so another client or a "
                "cancel may have cut the move short."
            )
        return actual

    def _await_reply(self, command: str, limit: float, hint: str = "") -> str:
        """Wait for a motion command's reply, watching for stop() and the limit.

        ``hint`` is added to the timeout message.
        """
        deadline = time.monotonic() + limit
        while True:
            if self._stop_requested.is_set():
                self._interrupted()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._abandon(command, limit, hint)
            try:
                reply = self._transport.read(min(remaining, _POLL_S))
            except DeviceTimeout:
                continue
            if self._stop_requested.is_set():
                # This OK may be the cancel's rather than the move's.
                self._interrupted()
            if reply == "FAIL":
                raise CommandRejected(f"{self!r} rejected {command}")
            return reply

    def _interrupted(self) -> NoReturn:
        """End a move that stop() cancelled."""
        self._settle(force=True)
        self._refresh_position()
        raise PositionError(f"{self!r} move was interrupted by stop()")

    def _abandon(self, command: str, limit: float, hint: str = "") -> NoReturn:
        """Stop a move that outlived its limit, then report the timeout."""
        self._safe_cancel(MODE_FIXED)
        self._refresh_position()
        raise DeviceTimeout(
            f"{self!r} {command} did not finish within {limit:g} s and was "
            f"cancelled; read position() before relying on where it stopped{hint}"
        )

    def _stop_idle(self) -> None:
        """stop() when no other thread is talking to the turntable."""
        # A jog this driver started is cancelled by mode. Otherwise motion of
        # an unknown mode (another process's) needs both cancels, spaced past
        # the window in which back-to-back commands lose an ack.
        modes = (MODE_JOG,) if self._jogging else (MODE_JOG, MODE_FIXED)
        for index, mode in enumerate(modes):
            if index:
                time.sleep(_CANCEL_SPACING_S)
            try:
                _check_ok(self._command(f"cancel({mode})", expect_ok=True), "cancel")
            except DeviceTimeout:
                # The ack can be lost even when the cancel lands (hardware
                # doc, rotate); a late one is drained before the next command.
                logger.debug("%r cancel(%d) was not acknowledged", self, mode)
        with self._state_lock:
            self._jogging = False

    def _safe_cancel(self, mode: int) -> None:
        """Best-effort cancel on an error path, where raising again helps nobody."""
        try:
            self._transport.send(f"cancel({mode})")
            with self._state_lock:
                self._strays += 1
            self._settle(force=True)
        except DeviceError:
            logger.debug("%r cancel after an error failed", self, exc_info=True)
        finally:
            with self._state_lock:
                self._jogging = False

    # -- protocol -----------------------------------------------------------------

    def _read_position(self) -> int:
        position = self._expect_int("get_position", 359)
        with self._state_lock:
            self._last_position = position
        return position

    def _refresh_position(self) -> None:
        """Re-read the position on an error path, keeping the original error."""
        try:
            self._read_position()
        except DeviceError:
            logger.debug("%r could not re-read its position", self, exc_info=True)

    def _expect_int(self, command: str, maximum: int | None = None) -> int:
        return self._parse_int(self._command(command), command, maximum)

    def _parse_int(self, reply: str, command: str, maximum: int | None = None) -> int:
        if not reply.isdigit() or (maximum is not None and int(reply) > maximum):
            limits = "an integer" if maximum is None else f"0-{maximum}"
            raise UnexpectedResponse(
                f"{self!r} expected {limits} for {command}, got {reply!r}"
            )
        return int(reply)

    def _command(self, command: str | bytes, *, expect_ok: bool = False) -> str:
        """Send one command and return its reply.

        Exactly one command is outstanding at a time; replies match commands
        by order alone (hardware doc, Command/response pairing).
        """
        self._settle()
        self._transport.send(command)
        try:
            while True:
                reply: str = self._transport.read(ACK_TIMEOUT_S)
                if reply == "OK" and not expect_ok and self._claim_stray():
                    continue
                break
        except DeviceTimeout:
            # A late reply must not be taken as the next command's.
            with self._state_lock:
                self._strays += 1
            raise
        if reply == "FAIL":
            raise CommandRejected(f"{self!r} rejected {_render(command)}")
        return reply

    def _claim_stray(self) -> bool:
        with self._state_lock:
            if self._strays:
                self._strays -= 1
                return True
            return False

    def _settle(self, force: bool = False) -> None:
        """Discard stray replies, waiting until the link has gone quiet."""
        with self._state_lock:
            pending = self._strays
        if not pending and not force:
            return
        deadline = time.monotonic() + ACK_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                reply = self._transport.read(_SETTLE_QUIET_S)
            except DeviceTimeout:
                break
            logger.debug("%r discarded stray reply %r", self, reply)
        self._transport.drain()
        with self._state_lock:
            self._strays = 0


def _check_ok(reply: str, what: str) -> None:
    if reply != "OK":
        raise UnexpectedResponse(f"expected OK for {what}, got {reply!r}")


def _angle_between(a: int, b: int) -> int:
    """Unsigned distance between two angles, in degrees, the short way round."""
    return abs((a - b + 180) % 360 - 180)


def _render(command: str | bytes) -> str:
    """Command text for an error message, with a binary payload abbreviated."""
    if isinstance(command, str):
        return command
    return command[:11].decode("ascii", "replace") + f"...({len(command)} B)"
