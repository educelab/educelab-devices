"""An emulation of the XLC2 firmware's serial protocol."""

XLC2_IDENTITY = "EduceLab XLC2,1.0,2.0.0"
XLC2_PIXELS = 222
# Fields each command takes.
ARITY = {"B": 1, "X": 1, "S": 4, "L": 2}


class XLCFirmware:
    """Answers commands the way XLC2 firmware 2.0.0 does.

    Pass an instance as a `ScriptedSerial` responder. ``pixels`` holds the
    strip's grey levels and ``xl4`` the 940 nm array's duty, both 0–255.
    """

    def __init__(self, identity: str = XLC2_IDENTITY) -> None:
        self.identity = identity
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
