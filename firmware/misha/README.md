# Setting up the MISHA LED panels

## Flashing the controller code
### Download Arduino software 
1. Download and install the Arduino IDE software for your windows computer: https://www.arduino.cc/en/software

###  Setup to program the Arduino Boards
1. Open Arduino IDE
2. Go to Tools → Manage Libraries...
  - Search for `Adafruit MCP23017` and install version 2.3.2+
  - Search for `SoftPWM` and install version 1.0.1+
3. Close the Library Manager
4. Go to Tools → Board → Arduino AVR Boards and select `Arduino Nano`
5. Go to Tools → Processor and select `ATmega328P`
6. Go to Tools → Port and select the device you want to program
7. Can now use the regular `Upload` button

### Flashing from the command line
With [`arduino-cli`](https://arduino.github.io/arduino-cli/), from this directory:

```shell
arduino-cli core install arduino:avr
arduino-cli lib install SoftPWM "Adafruit MCP23017 Arduino Library"
arduino-cli compile -b arduino:avr:nano:cpu=atmega328 MISHA
arduino-cli upload -b arduino:avr:nano:cpu=atmega328 -p <port> MISHA
```

On Apple silicon, the AVR compiler needs Rosetta 2 (`softwareupdate --install-rosetta`).

## Protocol
The board listens on its USB virtual COM port at 9600 baud. Commands are ASCII lines
ending in `\n`. Opening the port resets the board, which ignores commands for about
2 seconds while it boots.

### Identifying the device
Sending `ID` (or `?`) asks the board to identify itself. It replies with a single
comma-separated line, `name,hardware revision,software version`:

```
> ID
< MISHA LED Panels,1.1,2.0.0
```

Use this to confirm a serial port belongs to a MISHA board before sending LED commands.
The hardware revision tracks the PCB (currently 1.1); the software version tracks
`MISHA.ino` and is set by the `SW_VERSION` define at the top of that file.

### Commands
Only one wavelength is lit at a time, at an intensity from 0 to 100 percent.

| Command | Effect | Reply |
|---|---|---|
| `<nm>,<intensity>` | Lights `<nm>` at `<intensity>` and turns off the others. If `<nm>` is already lit, only its intensity changes, with no dark gap. | `OK` |
| `<nm>,0` | Turns off `<nm>` if it's lit. If another wavelength is lit, it stays on. | `OK` |
| `OFF` | Turns everything off. `0,0` does the same, for hosts written for 1.0.0. | `OK` |
| `STATE` | Reports the lit wavelength and its intensity, or `0,0` if all are off. | `OK <nm>,<intensity>` |

Every command is answered with exactly one line: `OK` (with the answer, for `STATE`) if
it was carried out, or `ERR <reason>` if it wasn't. A rejected command changes nothing.

| Example | Reply |
|---|---|
| `420,100` | `OK`: 420 nm at 100% |
| `590,50` | `OK`: 590 nm at 50%, 420 nm off |
| `590,80` | `OK`: 590 nm brightens to 80% |
| `420,0` | `OK`: nothing changes, 420 nm wasn't lit |
| `STATE` | `OK 590,80` |
| `OFF` | `OK`: everything off |
| `600,50` | `ERR unknown wavelength` |
| `590,101` | `ERR intensity out of range` |
| `590,` | `ERR invalid command` |

Wavelengths: 365, 385, 395, 420, 450, 470, 500, 530, 560, 590, 615, 630, 660, 730, 850,
and 940.

## Version history
- **2.0.0**: Every command is answered with `OK` or `ERR <reason>`. Unknown
  wavelengths, intensities above 100, and malformed commands are rejected without
  changing the LEDs; previously an unknown wavelength turned everything off silently,
  and only malformed commands got a reply (`Invalid command`). Added `STATE` and `OFF`.
  Changing the lit wavelength's intensity no longer blinks it off, and `<nm>,0` no
  longer turns off a different wavelength. The `ID` reply is unchanged. The
  `educelab-devices` MISHA driver requires this version.
- **1.0.0**: `ID` handshake added. Commands other than `ID` are not acknowledged.
