"""An emulation of the XLC1 and XLC2 firmware's serial protocol."""

XLC1_IDENTITY = "EduceLab XLC1,1.0,2.0.0"
XLC2_IDENTITY = "EduceLab XLC2,1.0,2.0.0"
XLC2_PIXELS = 222


class XLCFirmware:
    """Answers commands the way XLC1 or XLC2 firmware 2.0.0 does.

    Pass an instance as a `ScriptedSerial` responder. XLC1 keeps ``color``,
    an ``(r, g, b)`` of 0–255 duties. XLC2 keeps ``pixels``, the strip's
    grey levels, and ``xl4``, the 940 nm array's duty.

    Args:
        board: ``"XLC1"`` or ``"XLC2"``.
        identity: The handshake reply, for emulating other firmware.
    """

    def __init__(self, board: str = "XLC1", identity: str | None = None) -> None:
        self.board = board
        self.identity = identity or (
            XLC1_IDENTITY if board == "XLC1" else XLC2_IDENTITY
        )
        self.color = (0, 0, 0)
        self.pixels = [0] * XLC2_PIXELS
        self.xl4 = 0

    def __call__(self, line: str) -> list[str]:
        # The firmware only acts on the last complete <...> frame in a write.
        start, end = line.rfind("<"), line.rfind(">")
        if start < 0 or end < start:
            return []
        body = line[start + 1 : end]
        if body == "?" or body.upper() == "ID":
            return [self.identity]
        if len(body) > 31:
            return ["ERR invalid command"]
        command, *fields = body.split(",")
        arity = (
            {"C": 3} if self.board == "XLC1" else {"B": 1, "X": 1, "S": 4, "L": 2}
        ).get(command)
        if arity is None:
            return ["ERR unknown command"]
        values = [_level(field) for field in fields]
        if len(values) != arity or None in values:
            return ["ERR invalid command"]
        return self._apply(command, values)

    def _apply(self, command: str, values: list) -> list[str]:
        if command == "C":
            self.color = tuple(values)
        elif command == "B":
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
