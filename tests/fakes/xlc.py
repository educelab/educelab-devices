"""An emulation of the XLC2 firmware's serial protocol."""

import re

from educelab.devices._serial import parse_version

XLC2_PIXELS = 222
# Fields each command takes.
ARITY = {"B": 1, "X": 1, "S": 4, "L": 2}
UNIT_ID = re.compile(r"[A-Za-z0-9_-]{1,16}")


def identity(firmware: str = "2.1.0", unit_id: str | None = None) -> str:
    """The handshake reply of an XLC2 running ``firmware``."""
    if parse_version(firmware) < (2, 1, 0):
        return f"EduceLab XLC2,1.0,{firmware}"
    return f"EduceLab XLC2,1.0,{firmware},{unit_id or ''}"


XLC2_IDENTITY = identity()


class XLCFirmware:
    """Answers commands the way XLC2 firmware 2.1.0 (or 2.0.0) does.

    Pass an instance as a `ScriptedSerial` responder. ``pixels`` holds the
    strip's grey levels, ``xl4`` the 940 nm array's duty, both 0–255, and
    ``unit_id`` the ID in EEPROM. Setting ``identity`` replaces the handshake
    reply verbatim, e.g. to play older firmware.
    """

    def __init__(self, firmware: str = "2.1.0", unit_id: str | None = None) -> None:
        self.firmware = firmware
        self.unit_id = unit_id
        self.identity: str | None = None
        self.pixels = [0] * XLC2_PIXELS
        self.xl4 = 0

    def __call__(self, line: str) -> list[str]:
        # The firmware only acts on the last complete <...> frame in a write.
        start, end = line.rfind("<"), line.rfind(">")
        if start < 0 or end < start:
            return []
        body = line[start + 1 : end]
        if body == "?" or body.upper() == "ID":
            return [self.identity or identity(self.firmware, self.unit_id)]
        if len(body) > 31:
            return ["ERR invalid command"]
        command, *fields = body.split(",")
        if command == "U" and parse_version(self.firmware) >= (2, 1, 0):
            if len(fields) != 1 or not UNIT_ID.fullmatch(fields[0]):
                return ["ERR invalid command"]
            self.unit_id = fields[0]
            return ["OK"]
        if command not in ARITY:
            return ["ERR unknown command"]
        values = [_level(field) for field in fields]
        if len(values) != ARITY[command] or None in values:
            return ["ERR invalid command"]
        return self._apply(command, values)

    def _apply(self, command: str, values: list) -> list[str]:
        if command == "B":
            self.pixels = [values[0]] * XLC2_PIXELS
        elif command == "X":
            self.xl4 = values[0]
        elif command == "S":
            bounds = (0, 55, 111, 167, XLC2_PIXELS)
            self.pixels = [
                values[segment]
                for segment in range(4)
                for _ in range(bounds[segment], bounds[segment + 1])
            ]
        elif command == "L":
            index, level = values
            if index >= XLC2_PIXELS:
                return ["ERR index out of range"]
            self.pixels[index] = level
        return ["OK"]


def _level(text: str) -> int | None:
    text = text.strip(" ")
    if not 0 < len(text) <= 3 or not text.isdigit() or int(text) > 255:
        return None
    return int(text)
