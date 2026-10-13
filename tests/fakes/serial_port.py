"""A scripted stand-in for ``serial.Serial``."""

import time
from collections.abc import Callable, Iterable, Sequence

Responder = Callable[[str], Sequence[str]]


class ScriptedSerial:
    """Replays a serial device: each line written gets canned reply lines.

    Pass ``fake.open`` as a `SerialTransport` opener. Writes are checked
    against ``script`` in order, each entry being an expected line and the
    lines to reply with. Once the script runs out, ``respond`` (if given)
    answers every write, which suits emulating firmware for contract tests.
    Any other write fails the test.

    Args:
        script: ``(expected line, reply lines)`` pairs.
        respond: Answers writes after the script, returning reply lines.
        present: Whether the port exists. If not, opening it fails.
    """

    def __init__(
        self,
        script: Iterable[tuple[str, Sequence[str]]] = (),
        *,
        respond: Responder | None = None,
        present: bool = True,
    ) -> None:
        self.script = list(script)
        self.respond = respond
        self.present = present
        self.written: list[str] = []
        self.opened_with: tuple[str, int, float] | None = None
        self.is_open = False
        self.timeout: float | None = None
        self._pending: list[bytes] = []

    def open(self, port: str, baudrate: int, timeout: float) -> "ScriptedSerial":
        if not self.present:
            raise OSError(f"could not open port {port}: no such device")
        self.opened_with = (port, baudrate, timeout)
        self.timeout = timeout
        self.is_open = True
        return self

    def queue(self, *lines: str) -> None:
        """Make lines arrive unprompted, e.g. boot chatter."""
        self._pending.extend(f"{line}\r\n".encode() for line in lines)

    def unplug(self) -> None:
        """Make every later read or write fail as if the cable were pulled."""
        self.present = False

    def assert_done(self) -> None:
        """Fail if any scripted write hasn't happened."""
        remaining = [line for line, _ in self.script]
        assert not remaining, f"expected writes never came: {remaining}"

    # The parts of serial.Serial that SerialTransport uses.

    def reset_input_buffer(self) -> None:
        self._check_present()
        self._pending.clear()

    def write(self, data: bytes) -> int:
        self._check_present()
        line = data.decode("ascii").rstrip("\n")
        self.written.append(line)
        if self.script:
            expected, replies = self.script.pop(0)
            assert line == expected, f"expected {expected!r}, got {line!r}"
        elif self.respond is not None:
            replies = self.respond(line)
        else:
            raise AssertionError(f"unexpected write {line!r}")
        self.queue(*replies)
        return len(data)

    def flush(self) -> None:
        self._check_present()

    def readline(self) -> bytes:
        self._check_present()
        if self._pending:
            return self._pending.pop(0)
        time.sleep(self.timeout or 0)
        return b""

    def close(self) -> None:
        self.is_open = False

    def _check_present(self) -> None:
        if not self.present:
            self.is_open = False
            raise OSError("device disconnected")
