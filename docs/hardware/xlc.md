# XLC2 light controller

A lab-built LED controller for the `pgs-scan` gantry rigs: an Arduino-class board on
USB serial that drives a white DotStar pixel strip and the XL4 940 nm infrared array
(the "XMount" LEDs). Its firmware lives in [`firmware/xlc2/`](../../firmware/xlc2/). It
was copied from `pgs-scan` `dev` at `aed0b5e` (2022-11-01) and updated to 2.0.0 here
(see [Version history](#version-history)).

**XLC1**, an earlier controller for one RGB strip, is a separate device rather than an
older revision of this board. Its units have been retired (maintainer, 2026-10-29), so
this package has no XLC1 driver or firmware. Its 1.1 firmware is in `pgs-scan` and in
this repository's history (commit `3f85a42`).

Driver: `educelab.devices.lights.xlc.XLC2`. Manual check: `el-devices xlc`.

`discover()` finds nothing, and a board is opened by `port=` or by its USB
`serial_number=`. Nothing in the USB descriptors identifies an XLC2 (see
[Identity](#identity)), and opening a port to send the handshake resets the board
behind it, which could be the X-Carve's homed GRBL controller. The one XLC2 checked has
serial number `0000`, a placeholder, so `serial_number=` can't tell XLC2s apart. Revisit
this if the units' descriptors can be rewritten.

Unless marked otherwise, everything below is **assumed**: it is read from the firmware
source and from `pgs-scan`'s `XLC2` class (`pgs_scan/components/lights.py`, `dev`), not
observed on a board. The 2.0.0 replies were checked by compiling the sketch on a host
against stubbed Arduino libraries, not on a board.

## Identity

- **Handshake reply**: `EduceLab XLC2,1.0,2.0.0` from 2.0.0 on; `EduceLab XLC2 (FW 1.2.0)`
  before.
- **Hardware version**: `1.0`, the `HW_VERSION` define compiled into the firmware. Like
  MISHA's, it's a label, not something read from the board.
- **Lights**: an Adafruit DotStar strip, 60 LED/m, white 6000–6500 K (`XLSγ` in
  `pgs-scan`), and the EduceLab XL4 IR940 array (`XL4`). These are `pgs-scan`'s labels,
  not checked against the hardware.
- **USB IDs**: `pgs-scan` searches for VID `0x1A86` (the generic WCH CH340) or `0x04D9`
  (Holtek). Neither identifies an XLC2; the handshake is the only reliable way.

### How an XLC2 shows up on the host

**Verified** on one XLC2 on macOS, 2026-10-29, with `pyserial-ports -v`:

| | |
|---|---|
| Port | `/dev/cu.usbmodem00001` |
| USB VID:PID | `04D9:B534` |
| USB serial number | `0000` |
| USB description | `USB TO UART BRIDGE` |

`04D9:B534` is a Holtek HT42B534 USB-to-UART bridge (**assumed**, from the VID:PID).
Its serial number `0000` is a placeholder rather than a unique one, so it can't be told
apart from another XLC2, or from any other HT42B534, by its descriptors. Whether the
HT42B534's descriptors can be rewritten, as MISHA's CP2102N's were, is **unknown**;
Holtek's datasheet or configuration tool would settle it. The `usbmodem` name suggests
the bridge is a USB CDC device, which macOS names from the serial number (**assumed**).
Other units may have other bridges, since `pgs-scan` also searches for the CH340's VID.

### What the firmware drives

- A 222-pixel DotStar strip on hardware SPI, colour order BRG, always set to grey
  (`r = g = b`).
- The XL4 array, one PWM output on pin 6.
- Both start off at power-up.

## Transport

- 9600 baud, 8N1, through the board's USB virtual COM port.
- Opening the port resets the board (DTR). `pgs-scan` waits 2 s before sending the
  handshake. The firmware has no boot banner.
- Replies end in `\r\n` (Arduino `Serial.println`).

## Protocol (2.0.0)

Commands are framed by `<` and `>`. Anything outside a frame is ignored, so a trailing
line ending is harmless. A `<` inside a frame starts a new frame, dropping the
unfinished one. Fields are separated by commas, and spaces around a number are allowed.
A frame longer than 31 characters is rejected.

`<?>` or `<ID>` (any case) is the handshake. The reply is one line,
`name,hardware version,firmware version`, the same shape as MISHA's:

```
> <?>
< EduceLab XLC2,1.0,2.0.0
```

Every other command is answered with exactly one line: `OK` if it was carried out, or
`ERR <reason>` if it wasn't (ADR 0004). A rejected command changes nothing. Levels are
whole numbers from 0 to 255.

| Command | Effect |
|---|---|
| `<B,n>` | Every strip pixel to grey `n` |
| `<X,n>` | XL4 PWM duty `n` |
| `<S,a,b,c,d>` | Clears the strip, then sets four segments (pixels 0–54, 55–110, 111–166, 167–221) to grey `a`–`d` |
| `<L,i,n>` | Pixel `i` (0–221) to grey `n` |

The driver uses `<B>` for channel `white` and `<X>` for channel `940nm`.

| Reply | Cause |
|---|---|
| `ERR unknown command` | The command letter isn't one of these, or the frame is empty |
| `ERR invalid command` | Wrong number of fields, a value that isn't a number from 0 to 255, or a frame that's too long |
| `ERR index out of range` | `<L>` with an index past the strip |

## Protocol before 2.0.0 (FW 1.2.0)

Kept because boards in the field run it until they're reflashed. Framing is the same
as 2.0.0, except that the receive buffer holds 31 characters and extra characters
overwrite the last one. Values are clamped to 0–255 rather than rejected.

| Command | Effect | Lines sent back |
|---|---|---|
| `<?>` | Identify | `EduceLab XLC2 (FW 1.2.0)` |
| `<B,n>` | Every strip pixel to grey `n` | `Got a brightness command`, `Setting brightness to n` |
| `<X,n>` | XL4 PWM duty `n` | `Got a xmount command` |
| `<S,a,b,c,d>` | As in 2.0.0 | `Got a segment command` |
| `<L,i,n>` | Pixel `i` to grey `n` | `Got an index command` |
| anything else | Nothing | `Unknown Command`, `Unknown Command` |

`pgs-scan` uses only `<?>`, `<B,…>` and `<X,…>`.

No command is acknowledged in a form a host can rely on: some send one line, some two,
and an unknown command looks the same as a failure. Hazards of this firmware, fixed in
2.0.0:

- **Missing fields**: a command with too few fields (e.g. `<S,10>`) passes a null
  pointer to `atoi`. On AVR this reads whatever is at address 0, so the value is
  garbage rather than a crash (**assumed**).
- **`<L>`**: the index isn't checked. `Adafruit_DotStar::setPixelColor` ignores
  indices past the strip (**assumed**). `<L>` also shares its brightness variable with
  `<B>`.

## State and recovery

- Nothing persists. Both outputs are off after a reset, including the one caused by
  opening the port.
- The board can't report its outputs, so the driver's levels are the last ones it set.
  It turns both outputs off on open and close, as `pgs-scan` does.

## Hazards

- **Reflashing a board in use**: `pgs-scan` `dev` refuses 2.0.0 boards (see
  [Compatibility with `pgs-scan` `dev`](#compatibility-with-pgs-scan-dev)). Reflash a
  rig's board only when that rig moves to this package's drivers.

## Compatibility with `pgs-scan` `dev`

Checked by reading `pgs_scan/components/lights.py` on `dev` against the 2.0.0 replies,
2026-10-29:

- **Handshake**: `XLC2` matches `EduceLab XLC2 (FW 1.\d.\d)`, which the 2.0.0 reply
  doesn't, so `dev` logs `Invalid handshake response` and disconnects.
- **Commands**: `dev` sends only `<B,…>` and `<X,…>`, both still valid. It never reads
  replies after the handshake, so the `OK` lines would be ignored, as the old debug
  lines were.

## Version history

- **2.0.0**: every command is answered with `OK` or `ERR <reason>`, and the debug lines
  are gone. Values outside 0–255, missing or extra fields, pixel indices past the
  strip, and over-long frames are rejected without changing anything. The handshake
  reply is now `name,hardware version,firmware version`, like MISHA's, and `<ID>` is
  accepted as well as `<?>`. A `<` inside a frame starts a new one. The
  `educelab-devices` driver requires this version.
- **1.2.0**: as used by `pgs-scan` `dev` (see
  [Protocol before 2.0.0](#protocol-before-200-fw-120)).

## Flashing

Open `firmware/xlc2/XLC2_firmware/` in the Arduino IDE, or use
[`arduino-cli`](https://arduino.github.io/arduino-cli/). The sketch needs the
**Adafruit DotStar** library. It hasn't been compiled for a board yet, and the board
type is **unknown** (see [Unknowns](#unknowns)), so pick it in the IDE or pass its
FQBN:

```shell
arduino-cli lib install "Adafruit DotStar"
arduino-cli compile -b <fqbn> firmware/xlc2/XLC2_firmware
arduino-cli upload -b <fqbn> -p <port> firmware/xlc2/XLC2_firmware
```

## Unknowns

- The Arduino board in each XLC2, which decides the FQBN for flashing, and the
  USB-serial bridge in the units not yet checked. Settle by plugging in each unit and
  recording its `list_ports` entry.
- Whether the HT42B534's serial number and description can be rewritten, which
  would let `serial_number=` pick out one XLC2.
- The XL4's current and thermal limits at full duty.
- Whether any deployed unit runs firmware other than 1.2.0.

## Manual check

Not yet run. The track's final phase flashes the updated firmware on a rig and records
the results here.
