"""A scripted stand-in for the Foldio360's BLE transport."""

from collections.abc import Callable, Mapping

from educelab.devices.errors import DeviceTimeout

TIMEOUT = object()
"""A scripted reply that makes `FakeNusTransport.read()` time out."""

Reply = str | Callable[[], str] | object


class FakeNusTransport:
    """Replays scripted replies to commands, in order, and records what was sent.

    Args:
        replies: Reply per command text. A callable is called for each reply,
            `TIMEOUT` raises `DeviceTimeout`, and unscripted commands get
            ``"OK"``.
    """

    def __init__(self, replies: Mapping[str, Reply] | None = None) -> None:
        self.sent: list[str] = []
        self.connected = True
        self.address = "FA:KE"
        self.drained = 0
        self._replies = dict(replies or {})
        self._pending: str | None = None

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def send(self, command: str) -> None:
        self.sent.append(command)
        self._pending = command

    def read(self, timeout: float | None) -> str:
        key = self._pending
        reply = self._replies.get(key, "OK") if key is not None else "OK"
        if reply is TIMEOUT:
            raise DeviceTimeout(f"scripted timeout for {key!r}")
        if callable(reply):
            reply = reply()
        assert isinstance(reply, str)
        return reply

    def drain(self) -> None:
        self.drained += 1
