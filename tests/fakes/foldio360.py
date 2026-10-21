"""A fake Foldio360 transport that emulates the turntable's firmware."""

import queue
import re
import threading
import types
from collections.abc import Callable, Iterable, Mapping

from educelab.devices.errors import DeviceTimeout

FIRMWARE = "V1.0.2_1712M02669"

TIMEOUT = object()
"""A scripted reply that sends nothing, so the read times out."""

Reply = str | Callable[[], str] | object

_ROTATE = re.compile(r"rotate\((CW|CCW),(\d+),(\d+),(\d+)\)")
_CANCEL = re.compile(r"cancel\((\d+)\)")
_SET_BRIGHT = re.compile(r"set_bright\((-?\d+)\)")
_RUN = re.compile(r"run\((\d+),(\d+)\)")
_IR_SHUTTER = re.compile(r"ir_shutter\((\d+)\)")

# Frame counts the firmware completes; others wedge after frame 1.
_GOOD_FRAMES = (24, 36, 48)


class FakeNusTransport:
    """Stands in for `NusTransport`, replying the way firmware V1.0.2 does.

    It keeps the position counter and halo level, answers every command in the
    vocabulary, and answers FAIL to anything else. Replies queue up like
    notifications and are read in order.

    Args:
        replies: Replies that override the emulation, per command text. A
            callable is called for each reply, and `TIMEOUT` sends nothing.
        angle: Starting position counter, in degrees.
        hold_moves: Leave fixed-angle rotations running until `finish_move()`
            or a cancel, instead of finishing them at once.
        cancel_acks_move: Whether cancelling a held rotation also sends the
            rotation's own OK, as well as the cancel's. The real behavior is
            unknown (docs/hardware/foldio360.md, cancel).
        cancelled_fraction: How much of a held rotation is done when it is
            cancelled.
        lost_acks: Commands that take effect but whose reply never arrives,
            as under rapid writes (docs/hardware/foldio360.md, set_bright).
        ack_delays: Seconds to hold back each command's reply.
    """

    def __init__(
        self,
        replies: Mapping[str, Reply] | None = None,
        *,
        angle: int = 0,
        hold_moves: bool = False,
        cancel_acks_move: bool = True,
        cancelled_fraction: float = 0.5,
        lost_acks: Iterable[str] = (),
        ack_delays: Mapping[str, float] | None = None,
    ) -> None:
        self.sent: list[str | bytes] = []
        self.connected = False
        self.address = "FA:KE"
        self.drained = 0
        self.angle = angle
        self.brightness = 0
        self.jogging = False
        self.hold_moves = hold_moves
        self.cancel_acks_move = cancel_acks_move
        self.cancelled_fraction = cancelled_fraction
        self.moving: int | None = None
        """Signed degrees of a held rotation, while one is running."""
        self.move_started = threading.Event()
        self.signal: bytes | None = None
        self.frames: int | None = None
        """Frame count of the run sequence in progress."""
        self.frame = 0
        self.lost_acks = set(lost_acks)
        self.ack_delays = dict(ack_delays or {})
        self._replies = dict(replies or {})
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def send(self, command: str | bytes) -> None:
        text = command if isinstance(command, str) else command.decode("latin-1")
        with self._lock:
            self.sent.append(command)
            if text in self._replies:
                reply = self._replies[text]
                if reply is TIMEOUT:
                    return
                self._queue.put(reply() if callable(reply) else reply)  # type: ignore[arg-type]
                return
            replies = self._respond(text)
            if text in self.lost_acks:
                return
            delay = self.ack_delays.get(text)
            for reply in replies:
                if delay is None:
                    self._queue.put(reply)
                else:
                    threading.Timer(delay, self._queue.put, (reply,)).start()

    def read(self, timeout: float | None) -> str:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            raise DeviceTimeout(f"no notification within {timeout}s") from None

    def drain(self) -> None:
        self.drained += 1
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def notify(self, reply: str) -> None:
        """Queue a notification that no command asked for."""
        self._queue.put(reply)

    def finish_move(self) -> None:
        """Complete a held rotation and send its OK."""
        with self._lock:
            if self.moving is not None:
                self.angle = (self.angle + self.moving) % 360
                self.moving = None
                self._queue.put("OK")

    def _respond(self, text: str) -> list[str]:
        if text == "get_status":
            return [FIRMWARE]
        if text == "get_position":
            return [str(self.angle % 360)]
        if text == "get_bright":
            return [str(self.brightness)]
        if match := _ROTATE.fullmatch(text):
            return self._rotate(*match.groups())
        if _CANCEL.fullmatch(text):
            return self._cancel()
        if match := _RUN.fullmatch(text):
            if match.group(2) not in ("1", "2", "3"):
                return ["FAIL"]
            self.frames, self.frame = int(match.group(1)), 1
            return ["1"]
        if text == "continue":
            return self._continue()
        if match := _IR_SHUTTER.fullmatch(text):
            if self.signal is None or match.group(1) != "0":
                return ["FAIL"]
            return [str(self.frame or 1)]
        if text.startswith("set_signal(") and text.endswith(")"):
            self.signal = text[len("set_signal(") : -1].encode("latin-1")
            return ["OK"]
        if match := _SET_BRIGHT.fullmatch(text):
            level = int(match.group(1))
            if not 0 <= level <= 100:
                return ["FAIL"]
            self.brightness = level
            return ["OK"]
        return ["FAIL"]

    def _rotate(self, direction: str, degrees: str, speed: str, mode: str) -> list[str]:
        if speed not in ("1", "2", "3"):
            return ["FAIL"]
        if mode == "0":
            self.jogging = True
            return ["OK"]
        signed = int(degrees) if direction == "CW" else -int(degrees)
        if self.hold_moves:
            self.moving = signed
            self.move_started.set()
            return []
        self.angle = (self.angle + signed) % 360
        return ["OK"]

    def _continue(self) -> list[str]:
        if self.frames is None:
            return ["FAIL"]
        if self.frames not in _GOOD_FRAMES:
            return []  # wedged
        if self.frame == self.frames:
            self.frames, self.frame = None, 0
            return ["0"]
        self.frame += 1
        self.angle = (self.angle + 360 // self.frames) % 360
        return [str(self.frame)]

    def _cancel(self) -> list[str]:
        self.jogging = False
        self.frames, self.frame = None, 0
        replies = []
        if self.moving is not None:
            done = round(self.moving * self.cancelled_fraction)
            self.angle = (self.angle + done) % 360
            self.moving = None
            if self.cancel_acks_move:
                replies.append("OK")
        replies.append("OK")
        return replies


def fake_bleak(
    advertisers: Iterable[tuple[str, str | None, tuple[str, ...], int | None]] = (),
    *,
    connect_error: BaseException | None = None,
    scan_error: BaseException | None = None,
) -> Callable[[], types.SimpleNamespace]:
    """A stand-in for the bleak module, to replace the driver's ``_bleak``.

    Args:
        advertisers: ``(address, name, service_uuids, rssi)`` for each device
            a scan finds.
        connect_error: What connecting to any address raises.
        scan_error: What scanning raises.
    """
    found = {
        address: (
            types.SimpleNamespace(address=address, name=name),
            types.SimpleNamespace(local_name=name, service_uuids=uuids, rssi=rssi),
        )
        for address, name, uuids, rssi in advertisers
    }

    class BleakScanner:
        @staticmethod
        async def discover(timeout: float, return_adv: bool) -> dict:
            if scan_error is not None:
                raise scan_error
            return found

    class BleakClient:
        def __init__(self, address: str, **kwargs: object) -> None:
            self.address = address

        async def connect(self) -> None:
            if connect_error is not None:
                raise connect_error

    module = types.SimpleNamespace(BleakScanner=BleakScanner, BleakClient=BleakClient)
    return lambda: module
