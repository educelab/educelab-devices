"""An emulation of GRBL 1.1 on an X-Carve, behind a serial port.

Motion runs on the wall clock (scaled by ``speedup``) and is evaluated lazily
whenever the port is read or written, so the fake needs no thread of its own.
"""

import math
import re
import threading
import time
from collections.abc import Sequence

BANNER = "Grbl 1.1g ['$' for help]"
LOCKED = "[MSG:'$H'|'$X' to unlock]"
AXES = "XYZ"

# Real-time commands, acted on as soon as they arrive.
STATUS = ord("?")
FEED_HOLD = ord("!")
CYCLE_START = ord("~")
SOFT_RESET = 0x18
JOG_CANCEL = 0x85

# GRBL error codes the fake can send.
ERR_UNSUPPORTED = 20
ERR_LOCKED = 9
ERR_HOMING_DISABLED = 5
ERR_BAD_JOG = 16
ERR_NO_FEED = 22
ERR_TRAVEL = 15

# $-settings an X-Carve reports, from pgs-scan's XController where it says.
SETTINGS = {
    10: 1.0,  # status reports give MPos
    20: 0.0,  # soft limits off
    22: 1.0,  # homing on
    27: 1.0,  # homing pull-off, mm
    110: 10000.0,  # max rate, mm/min
    111: 10000.0,
    112: 750.0,
    130: 700.0,  # max travel, mm
    131: 700.0,
    132: 46.0,
}


class _Segment:
    """One queued jog: a straight line from ``start`` to ``end``."""

    def __init__(self, end: tuple[float, ...], feed: float) -> None:
        self.end = end
        self.feed = feed
        self.start: tuple[float, ...] | None = None
        self.started_at = 0.0
        self.duration = 0.0


class GrblMachine:
    """A GRBL 1.1 controller that a `SerialTransport` can open as a port.

    Pass ``machine.open`` as the transport's opener. Opening behaves like the
    port's DTR line resetting the board: the banner is sent, and with homing
    on the machine is alarm-locked until ``$H`` or ``$X``.

    Args:
        position: Machine position at power-up, in mm.
        home: Machine position after ``$H``, in mm.
        homing_s: How long ``$H`` takes, in (unscaled) seconds.
        speedup: How many times faster than real motion runs.
        settings: $-settings to change from `SETTINGS`.
        banner_on_open: Whether opening resets the board. If not, only a
            soft reset makes it send the banner.
        boot_s: How long the board takes to boot after the port opens. It
            ignores writes until then.
        present: Whether the port exists.
    """

    def __init__(
        self,
        *,
        position: Sequence[float] = (0.0, 0.0, 0.0),
        home: Sequence[float] = (-1.0, -1.0, -1.0),
        homing_s: float = 0.02,
        speedup: float = 1000.0,
        settings: dict[int, float] | None = None,
        banner_on_open: bool = True,
        boot_s: float = 0.01,
        present: bool = True,
    ) -> None:
        self.settings = {**SETTINGS, **(settings or {})}
        self.position = tuple(float(p) for p in position)
        self.home_position = tuple(float(p) for p in home)
        self.homing_s = homing_s
        self.speedup = speedup
        self.banner_on_open = banner_on_open
        self.boot_s = boot_s
        self._booting_until: float | None = None
        self.present = present
        self.state = "Idle"
        self.lines: list[str] = []
        self.realtime: list[int] = []
        self.opened_with: tuple[str, int, float] | None = None
        self.is_open = False
        self.timeout: float | None = None
        # Answers the next line with this error code instead of acting on it.
        self.reject_next: int | None = None
        # Ignores "?", like a controller too busy to answer.
        self.mute_status = False
        self._queue: list[_Segment] = []
        self._homing_until: float | None = None
        self._output: list[str] = []
        self._pending = b""
        self._cond = threading.Condition()
        if self.settings[22]:
            self.state = "Alarm"

    # Test controls

    def alarm(self, code: int) -> None:
        """Raise ``ALARM:code`` now, e.g. 1 for a hard limit."""
        with self._cond:
            self._advance()
            self._halt()
            self._homing_until = None
            self.state = "Alarm"
            self._say(f"ALARM:{code}")

    def unplug(self) -> None:
        """Make every later read or write fail as if the cable were pulled."""
        with self._cond:
            self.present = False
            self._cond.notify_all()

    @property
    def moving(self) -> bool:
        with self._cond:
            self._advance()
            return bool(self._queue)

    def current_position(self) -> tuple[float, ...]:
        with self._cond:
            self._advance()
            return self._now_position()

    # The parts of serial.Serial that SerialTransport uses.

    def open(self, port: str, baudrate: int, timeout: float) -> "GrblMachine":
        if not self.present:
            raise OSError(f"could not open port {port}: no such device")
        with self._cond:
            self.opened_with = (port, baudrate, timeout)
            self.timeout = timeout
            self.is_open = True
            if self.banner_on_open:
                self._halt()
                self._booting_until = time.monotonic() + self.boot_s
        return self

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._check_present()
            self._output.clear()

    def write(self, data: bytes) -> int:
        with self._cond:
            self._check_present()
            self._advance()
            if self._booting_until is not None:
                return len(data)
            for byte in data:
                if byte in (STATUS, FEED_HOLD, CYCLE_START, SOFT_RESET, JOG_CANCEL):
                    self.realtime.append(byte)
                    self._realtime(byte)
                elif byte == ord("\n"):
                    line = self._pending.decode("ascii").strip()
                    self._pending = b""
                    if line:
                        self.lines.append(line)
                        self._line(line)
                elif byte != ord("\r"):
                    self._pending += bytes([byte])
            self._cond.notify_all()
        return len(data)

    def flush(self) -> None:
        with self._cond:
            self._check_present()

    def readline(self) -> bytes:
        with self._cond:
            deadline = time.monotonic() + (self.timeout or 0)
            while True:
                self._check_present()
                self._advance()
                if self._output:
                    return f"{self._output.pop(0)}\r\n".encode()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cond.wait(min(remaining, self._next_event()))

    def close(self) -> None:
        with self._cond:
            self.is_open = False

    # Emulation

    def _check_present(self) -> None:
        if not self.present:
            self.is_open = False
            raise OSError("device disconnected")

    def _say(self, line: str) -> None:
        self._output.append(line)
        self._cond.notify_all()

    def _reset(self) -> None:
        """Soft reset or power-up: motion stops and homing is lost."""
        self._halt()
        self._homing_until = None
        self._output.clear()
        self.state = "Alarm" if self.settings[22] else "Idle"
        self._say("")
        self._say(BANNER)
        if self.state == "Alarm":
            self._say(LOCKED)

    def _halt(self) -> None:
        """Stop where the machine is and drop queued jogs."""
        self.position = self._now_position()
        self._queue.clear()
        if self.state == "Jog":
            self.state = "Idle"

    def _realtime(self, byte: int) -> None:
        if byte == STATUS:
            if not self.mute_status:
                self._say(self._status())
        elif byte == SOFT_RESET:
            self._reset()
        elif byte in (JOG_CANCEL, FEED_HOLD) and self.state == "Jog":
            # A feed hold during a jog cancels it (GRBL 1.1 jogging docs).
            self._halt()

    def _line(self, line: str) -> None:
        if self.reject_next is not None:
            code, self.reject_next = self.reject_next, None
            self._say(f"error:{code}")
            return
        if line == "$$":
            for number, value in sorted(self.settings.items()):
                self._say(f"${number}={value:.3f}")
            self._say("ok")
        elif line == "$X":
            if self.state == "Alarm":
                self.state = "Idle"
                self._say("[MSG:Caution: Unlocked]")
            self._say("ok")
        elif line == "$H":
            if not self.settings[22]:
                self._say(f"error:{ERR_HOMING_DISABLED}")
                return
            self._halt()
            self.state = "Home"
            self._homing_until = time.monotonic() + self.homing_s
        elif line.startswith("$J="):
            self._jog(line[3:])
        else:
            self._say(f"error:{ERR_UNSUPPORTED}")

    def _jog(self, words: str) -> None:
        if self.state in ("Alarm", "Home"):
            self._say(f"error:{ERR_LOCKED}")
            return
        parsed = re.findall(r"([A-Z])(-?\d+(?:\.\d*)?)", words.replace(" ", ""))
        values = dict(parsed)
        relative = "G91" in words
        if "F" not in values:
            self._say(f"error:{ERR_NO_FEED}")
            return
        if not any(axis in values for axis in AXES):
            self._say(f"error:{ERR_BAD_JOG}")
            return
        base = self._queue[-1].end if self._queue else self._now_position()
        end = tuple(
            (base[i] + float(values[axis]) if relative else float(values[axis]))
            if axis in values
            else base[i]
            for i, axis in enumerate(AXES)
        )
        if self.settings[20] and not self._within_travel(end):
            self._say(f"error:{ERR_TRAVEL}")
            return
        self._queue.append(_Segment(end, float(values["F"])))
        self._start_next()
        self._say("ok")

    def _within_travel(self, end: tuple[float, ...]) -> bool:
        return all(-self.settings[130 + i] <= end[i] <= 0.0 for i in range(len(AXES)))

    def _start_next(self, at: float | None = None) -> None:
        """Start the first queued jog, at ``at`` (default now)."""
        if not self._queue or self._queue[0].start is not None:
            return
        segment = self._queue[0]
        segment.start = self.position
        segment.started_at = time.monotonic() if at is None else at
        deltas = [abs(e - s) for s, e in zip(segment.start, segment.end, strict=True)]
        # mm/min, capped so no axis exceeds its own maximum rate.
        minutes = max(
            math.hypot(*deltas) / segment.feed,
            *(d / self.settings[110 + i] for i, d in enumerate(deltas)),
        )
        segment.duration = minutes * 60 / self.speedup
        self.state = "Jog"

    def _advance(self) -> None:
        """Bring motion and homing up to the present."""
        now = time.monotonic()
        if self._booting_until is not None and now >= self._booting_until:
            self._booting_until = None
            self._reset()
        if self._homing_until is not None and now >= self._homing_until:
            self._homing_until = None
            self.position = self.home_position
            self.state = "Idle"
            self._say("ok")
        while self._queue and self._queue[0].start is not None:
            segment = self._queue[0]
            finished_at = segment.started_at + segment.duration
            if now < finished_at:
                return
            self.position = segment.end
            self._queue.pop(0)
            if self._queue:
                self._start_next(at=finished_at)
            else:
                self.state = "Idle"

    def _next_event(self) -> float:
        """Seconds until homing or the current jog finishes."""
        ends = []
        if self._booting_until is not None:
            ends.append(self._booting_until)
        if self._homing_until is not None:
            ends.append(self._homing_until)
        if self._queue and self._queue[0].start is not None:
            ends.append(self._queue[0].started_at + self._queue[0].duration)
        if not ends:
            return 0.05
        return max(0.0, min(ends) - time.monotonic())

    def _now_position(self) -> tuple[float, ...]:
        if not self._queue or self._queue[0].start is None:
            return self.position
        segment = self._queue[0]
        assert segment.start is not None
        done = (time.monotonic() - segment.started_at) / (segment.duration or 1)
        done = min(max(done, 0.0), 1.0)
        return tuple(
            s + (e - s) * done for s, e in zip(segment.start, segment.end, strict=True)
        )

    def _status(self) -> str:
        x, y, z = self._now_position()
        return f"<{self.state}|MPos:{x:.3f},{y:.3f},{z:.3f}|FS:0,0>"
