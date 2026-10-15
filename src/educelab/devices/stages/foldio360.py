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
import queue
import re
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any, Self, TypeVar

from educelab.devices.errors import (
    CommandRejected,
    DeviceError,
    DeviceNotFound,
    DeviceTimeout,
    NotConnected,
    import_optional,
)

__all__ = [
    "CCW",
    "CW",
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

# Timeout sentinel: None disables the watchdog, AUTO derives it from the timing
# model, and a number is taken as seconds.
AUTO = -1

# The four reply shapes (hardware doc, Response grammar). Anything else is the
# head of a fragmented reply.
_RESPONSE = re.compile(r"^(?:OK|FAIL|\d+|V\d+\.\d+\.\d+_\S+)$")

# Replies have no terminator, so a fragment can only be waited for. Observed
# notifications all arrived whole within ~500 ms of the write.
_FRAGMENT_GRACE_S = 0.25

# A complete `3` and the head of `302` are the same bytes, so a digits-only
# reply gets a brief settle before it is believed.
_NUMERIC_SETTLE_S = 0.030

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

    def send(self, command: str) -> None:
        """Write a command, chunked at the 20-byte payload ceiling.

        Uses unacknowledged writes, matching the official app.
        """
        if not self.connected or self._loop is None:
            raise NotConnected("Foldio360 is not connected; call connect() first")
        logger.debug("-> %r", command)
        self._loop.call(self._write(command.encode("ascii")), timeout=10.0)

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


class Foldio360:
    """The Foldio360 turntable and its halo LED ring.

    Exactly one command is outstanding at a time, and every method blocks until
    the device replies; for a move that is when motion finishes, which can take
    most of a minute. Only one BLE central may hold the link, so close the
    official app before connecting.

    Args:
        address: BLE address, or ``None`` to scan for the turntable.
        name: Advertised name to match when scanning.
        scan_timeout: Seconds to scan for.
        transport: Replaces the BLE transport; tests inject a scripted fake.
        connect_timeout: Seconds to wait for a connection, or ``None`` for
            bleak's default.
    """

    def __init__(
        self,
        address: str | None = None,
        name: str = DEVICE_NAME,
        scan_timeout: float = 8.0,
        transport: Any = None,
        connect_timeout: float | None = None,
    ) -> None:
        self._transport = transport or NusTransport(
            address, name, scan_timeout, connect_timeout
        )
        self._jogging = False
        # Set by a write sent without reading its reply. The firmware usually
        # drops that ack under rapid writes, but when one does arrive it must be
        # cleared before the next command mistakes it for its own.
        self._unacked = False
        self._origin: int | None = None

    @property
    def origin(self) -> int | None:
        """The device angle that counts as zero, or ``None`` if none is pinned.

        ``None`` and ``0`` are different answers: only one means the caller
        pinned something that a power cycle will silently invalidate. Assigning
        an int wraps it into 0..359; anything else raises `TypeError`.
        """
        return self._origin

    @origin.setter
    def origin(self, value: int | None) -> None:
        if value is None:
            self._origin = None
            return
        if not isinstance(value, int):
            raise TypeError(f"origin must be an int or None, got {value!r}")
        self._origin = value % 360

    @property
    def jogging(self) -> bool:
        """Whether a jog this client started is still turning."""
        return self._jogging

    @property
    def connected(self) -> bool:
        return bool(self._transport.connected)

    @property
    def address(self) -> str | None:
        return getattr(self._transport, "address", None)

    def connect(self) -> Self:
        self._transport.connect()
        return self

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> Self:
        return self.connect()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- queries ------------------------------------------------------------

    def get_status(self, timeout: float | None = AUTO) -> str:
        """Return the firmware string, e.g. ``"V1.0.2_1712M02669"``."""
        return self._command("get_status", timeout, ACK_TIMEOUT_S)

    def get_position(self, timeout: float | None = AUTO) -> int:
        """Return the platter's angle, 0-359 clockwise, relative to `origin`."""
        return (self.get_device_position(timeout) - (self.origin or 0)) % 360

    def get_device_position(self, timeout: float | None = AUTO) -> int:
        """Return the firmware's own angle, 0-359, counted clockwise.

        The counter integrates commanded motion (it can't see the platter) and
        re-zeros at power-on; see the hardware doc, State and recovery.
        """
        return self._expect_index("get_position", timeout, ACK_TIMEOUT_S)

    def set_origin(
        self, device_angle: int | None = None, timeout: float | None = AUTO
    ) -> int:
        """Declare which device angle counts as zero, and return it.

        With no argument, pins the platter's current position. The origin lives
        only in this object, and a power cycle silently invalidates it.
        """
        if device_angle is None:
            device_angle = self.get_device_position(timeout)
        self.origin = device_angle
        return device_angle % 360

    def clear_origin(self) -> None:
        """Forget the pinned origin; positions read the device's counter again."""
        self.origin = None

    def get_brightness(self, timeout: float | None = AUTO) -> int:
        """Return the halo level, 0-100."""
        return self._expect_index("get_bright", timeout, ACK_TIMEOUT_S)

    # -- motion -------------------------------------------------------------

    def rotate(
        self, direction: str, degrees: int, speed: int = 1, timeout: float | None = AUTO
    ) -> None:
        """Turn a fixed angle and stop, blocking until the motion completes.

        Angles above 360 are executed literally. If the watchdog expires, the
        motor is stopped before `DeviceTimeout` is raised.
        """
        _check_direction(direction)
        _check_speed(speed)
        if not isinstance(degrees, int) or degrees < 1:
            raise ValueError(
                f"degrees must be an int >= 1 for a fixed-angle move, got "
                f"{degrees!r}; the firmware's response to 0 is untested"
            )
        self._expect_ok(
            f"rotate({direction},{degrees},{speed},{MODE_FIXED})",
            timeout,
            watchdog_seconds(degrees, speed),
            cancel_mode=MODE_FIXED,
        )

    def rotate_by(
        self, degrees: int, speed: int = 1, timeout: float | None = AUTO
    ) -> None:
        """Turn a signed angle: positive clockwise, negative counter-clockwise.

        Zero is a no-op, since the firmware's response to it is untested.
        """
        if not isinstance(degrees, int):
            raise TypeError(f"degrees must be an int, got {degrees!r}")
        if degrees == 0:
            return
        direction = CW if degrees > 0 else CCW
        self.rotate(direction, abs(degrees), speed=speed, timeout=timeout)

    def move_to(self, angle: int, speed: int = 1, timeout: float | None = AUTO) -> int:
        """Turn to an absolute angle, 0-359, the shorter way round.

        Returns the signed angle travelled.
        """
        if not isinstance(angle, int):
            raise TypeError(f"angle must be an int, got {angle!r}")
        # Shortest signed path in [-180, 180); an exact half-turn goes CCW.
        delta = (angle % 360 - self.get_position() + 540) % 360 - 180
        self.rotate_by(delta, speed=speed, timeout=timeout)
        return delta

    def home(self, speed: int = 1, timeout: float | None = AUTO) -> int:
        """Turn to `origin` (or the firmware's zero) the shorter way round."""
        return self.move_to(0, speed=speed, timeout=timeout)

    def jog(self, direction: str, speed: int = 1, timeout: float | None = AUTO) -> None:
        """Start turning continuously and return; `cancel()` stops it.

        The device acknowledges on spin-up, not on cancel.
        """
        _check_direction(direction)
        _check_speed(speed)
        self._expect_ok(
            f"rotate({direction},0,{speed},{MODE_JOG})",
            timeout,
            ACK_TIMEOUT_S,
            cancel_mode=MODE_JOG,
        )
        self._jogging = True

    def cancel(self, mode: int | None = None, timeout: float | None = AUTO) -> None:
        """Stop motion. Safe and idempotent even when nothing is moving.

        ``mode`` mirrors the move being cancelled; left unset, it is the jog
        mode if this client started a jog and the fixed mode otherwise.
        """
        if mode is None:
            mode = MODE_JOG if self._jogging else MODE_FIXED
        self._expect_ok(f"cancel({mode})", timeout, ACK_TIMEOUT_S)
        self._jogging = False

    def stop(self, timeout: float | None = AUTO) -> None:
        """Halt whatever is moving, without knowing which mode it is.

        Sends both cancels, spaced past the window where back-to-back commands
        can lose an acknowledgement.
        """
        self.cancel(MODE_JOG, timeout=timeout)
        time.sleep(_CANCEL_SPACING_S)
        self.cancel(MODE_FIXED, timeout=timeout)

    # -- lighting -----------------------------------------------------------

    def set_brightness(
        self, level: int, wait: bool = True, timeout: float | None = AUTO
    ) -> None:
        """Set the halo LED ring, 0-100. Takes effect immediately.

        Pass ``wait=False`` when ramping: the device drops acks under rapid
        writes even though the values land.
        """
        if not isinstance(level, int) or not 0 <= level <= 100:
            raise ValueError(f"brightness must be an int in 0..100, got {level!r}")
        command = f"set_bright({level})"
        if not wait:
            self._transport.send(command)
            self._unacked = True
            return
        self._expect_ok(command, timeout, ACK_TIMEOUT_S)

    # -- plumbing -----------------------------------------------------------

    def _expect_ok(
        self,
        command: str,
        timeout: float | None,
        auto: float,
        cancel_mode: int | None = None,
    ) -> None:
        reply = self._command(command, timeout, auto, cancel_mode)
        if reply != "OK":
            raise UnexpectedResponse(f"expected OK for {command}, got {reply!r}")

    def _expect_index(
        self,
        command: str,
        timeout: float | None,
        auto: float,
        cancel_mode: int | None = None,
    ) -> int:
        reply = self._command(command, timeout, auto, cancel_mode)
        try:
            return int(reply)
        except ValueError:
            raise UnexpectedResponse(
                f"expected an integer for {command}, got {reply!r}"
            ) from None

    def _command(
        self,
        command: str,
        timeout: float | None,
        auto: float,
        cancel_mode: int | None = None,
    ) -> str:
        """Send one command and wait for its reply."""
        if self._unacked:
            # A stray ack from a fire-and-forget write would be read as this
            # command's reply, since replies match commands by order alone.
            self._transport.drain()
            self._unacked = False
        resolved = auto if timeout == AUTO else timeout
        self._transport.send(command)
        try:
            reply: str = self._transport.read(resolved)
        except DeviceTimeout:
            # Sync is lost with a machine that has moving parts: stop the motor
            # first, then drop any late reply so the next command can't take it.
            if cancel_mode is not None:
                self._safe_cancel(cancel_mode)
            self._transport.drain()
            raise
        if reply == "FAIL":
            raise CommandRejected(f"Foldio360 rejected {command}")
        return reply

    def _safe_cancel(self, mode: int) -> None:
        """Best-effort stop on an error path, where raising again helps nobody."""
        try:
            self._transport.send(f"cancel({mode})")
            self._transport.read(ACK_TIMEOUT_S)
        except DeviceError:
            logger.debug("Foldio360 cancel after timeout failed", exc_info=True)
        finally:
            self._jogging = False
            self._transport.drain()


def _check_direction(direction: str) -> None:
    if direction not in (CW, CCW):
        raise ValueError(
            f"direction must be {CW!r} or {CCW!r}, got {direction!r}; note the "
            "official app labels CW 'Left'"
        )


def _check_speed(speed: int) -> None:
    if speed not in SPEEDS:
        raise ValueError(f"speed must be 1, 2 or 3, got {speed!r}")
