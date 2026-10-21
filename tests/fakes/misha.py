"""An emulation of the MISHA firmware's serial protocol."""

IDENTITY = "MISHA LED Panels,1.1,2.0.0"
WAVELENGTHS_NM = (
    365, 385, 395, 420, 450, 470, 500, 530,
    560, 590, 615, 630, 660, 730, 850, 940,
)  # fmt: skip


class MishaFirmware:
    """Answers commands the way MISHA firmware 2.0.0 does.

    Pass an instance as a `ScriptedSerial` responder. ``lit`` is the lit
    wavelength and intensity in percent, ``(0, 0)`` when all are off.
    """

    def __init__(self, identity: str = IDENTITY) -> None:
        self.identity = identity
        self.lit = (0, 0)

    def __call__(self, line: str) -> list[str]:
        if line.upper() == "ID" or line == "?":
            return [self.identity]
        if line.upper() == "STATE":
            return [f"OK {self.lit[0]},{self.lit[1]}"]
        if line.upper() == "OFF":
            self.lit = (0, 0)
            return ["OK"]
        wavelength, _, intensity = line.partition(",")
        if not (_is_number(wavelength) and _is_number(intensity)):
            return ["ERR invalid command"]
        nm, percent = int(wavelength), int(intensity)
        if percent > 100:
            return ["ERR intensity out of range"]
        if nm == 0:
            self.lit = (0, 0)
        elif nm not in WAVELENGTHS_NM:
            return ["ERR unknown wavelength"]
        elif percent > 0:
            self.lit = (nm, percent)
        elif self.lit[0] == nm:
            self.lit = (0, 0)
        return ["OK"]


def _is_number(text: str) -> bool:
    text = text.strip()
    return 0 < len(text) <= 4 and text.isdigit()
