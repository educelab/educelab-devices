# XLC1 and XLC2 light controllers

Lab-built LED controllers for the `pgs-scan` gantry rigs, each an Arduino-class board on
USB serial. **XLC1** drives one RGB LED strip. **XLC2** drives a white DotStar pixel
strip and the XL4 940 nm infrared array (the "XMount" LEDs). The firmware for both lives in
[`firmware/xlc1/`](../../firmware/xlc1/) and [`firmware/xlc2/`](../../firmware/xlc2/).
It was copied from `pgs-scan` `dev` at `aed0b5e` (2022-11-01) and updated to 2.0.0 here
(see [Version history](#version-history)).

The two boards are separate devices, not revisions of one board, so each reports
hardware version `1.0`.

Driver: `educelab.devices.lights.xlc` (`XLC1`, `XLC2`). Manual check: planned,
`el-devices xlc`.

`discover()` finds nothing, and a board is opened by `port=` or by its USB
`serial_number=`. Nothing in the USB descriptors identifies an XLC (see
[Identity](#identity)), and opening a port to send the handshake resets the board
behind it, which could be the X-Carve's homed GRBL controller. Revisit this once the
units' USB descriptors are recorded.

Unless marked otherwise, everything below is **assumed**: it is read from the firmware
sources and from `pgs-scan`'s `XLC1`/`XLC2` classes (`pgs_scan/components/lights.py`,
`dev`), not observed on a board. The 2.0.0 replies were checked by compiling both
sketches on a host against stubbed Arduino libraries, not on a board.

## Identity

| | XLC1 | XLC2 |
|---|---|---|
| Handshake reply (2.0.0) | `EduceLab XLC1,1.0,2.0.0` | `EduceLab XLC2,1.0,2.0.0` |
| Handshake reply (before 2.0.0) | `EduceLab XLC1 (FW 1.1)` | `EduceLab XLC2 (FW 1.2.0)` |
| Light | PLT Solutions 5050-60-IP65-RGB2450 Nano RGB strip (`XLSα` in `pgs-scan`) | Adafruit DotStar 60 LED/m, white 6000–6500 K (`XLSγ`), and EduceLab XL4 IR940 (`XL4`) |
| USB VID searched by `pgs-scan` | `0x04D9` | `0x1A86` or `0x04D9` |

The light descriptions are `pgs-scan`'s labels, not checked against the hardware. Nobody
has recorded which Arduino board or USB-serial bridge each unit has. `0x1A86` is the
generic WCH CH340 vendor ID, and `0x04D9` belongs to Holtek, so neither identifies an XLC.
The handshake is the only reliable way to identify one.

### What the firmware drives

- **XLC1**: PWM on Arduino pins 3 (red), 5 (green) and 6 (blue), through `analogWrite`.
  There is no separate brightness output.
- **XLC2**: a 222-pixel DotStar strip on hardware SPI, colour order BRG, always set to
  grey (`r = g = b`). The XL4 array is one PWM output on pin 6. The strip starts dark
  and the XL4 starts at 0.

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

| Board | Command | Effect |
|---|---|---|
| XLC1 | `<C,r,g,b>` | Sets the PWM duty of each colour |
| XLC2 | `<B,n>` | Every strip pixel to grey `n` |
| XLC2 | `<X,n>` | XL4 PWM duty `n` |
| XLC2 | `<S,a,b,c,d>` | Clears the strip, then sets four segments (pixels 0–54, 55–110, 111–166, 167–221) to grey `a`–`d` |
| XLC2 | `<L,i,n>` | Pixel `i` (0–221) to grey `n` |

| Reply | Cause |
|---|---|
| `ERR unknown command` | The command letter isn't one this board has, or the frame is empty. XLC1 no longer accepts `<B,n>`, which never did anything. |
| `ERR invalid command` | Wrong number of fields, a value that isn't a number from 0 to 255, or a frame that's too long |
| `ERR index out of range` | `<L>` with an index past the strip |

## Protocol before 2.0.0 (XLC1 FW 1.1, XLC2 FW 1.2.0)

Kept because boards in the field still run it until they're reflashed. Framing is the
same as 2.0.0, except that the receive buffer holds 31 characters and extra characters
overwrite the last one. Values aren't checked.

### XLC1

| Command | Effect | Lines sent back |
|---|---|---|
| `<?>` | Identify | `EduceLab XLC1 (FW 1.1)` |
| `<C,r,g,b>` | Sets the PWM duty of each colour, 0–255 | `Got a color command`, `Setting color to r, g, b` |
| `<B,n>` | Nothing | `Got a brightness command`, `Setting brightness to n` |
| anything else | Nothing | `Unknown Command`, `Unknown command` |

### XLC2

| Command | Effect | Lines sent back |
|---|---|---|
| `<?>` | Identify | `EduceLab XLC2 (FW 1.2.0)` |
| `<B,n>` | Every strip pixel to grey `n` (clamped 0–255) | `Got a brightness command`, `Setting brightness to n` |
| `<X,n>` | XL4 PWM duty `n` (clamped 0–255) | `Got a xmount command` |
| `<S,a,b,c,d>` | Clears the strip, then sets four segments (pixels 0–54, 55–110, 111–166, 167–221) to grey `a`–`d` | `Got a segment command` |
| `<L,i,n>` | Pixel `i` to grey `n` | `Got an index command` |
| anything else | Nothing | `Unknown Command`, `Unknown Command` |

`pgs-scan` uses only `<?>`, `<C,…>` (XLC1), `<B,…>` and `<X,…>` (XLC2).

Neither board acknowledges a command in a form a host can rely on. Some commands send
one line, some send two, and an unknown command looks the same as a failure.

Hazards of this firmware, all fixed in 2.0.0:

- **Missing fields**: a command with too few fields (e.g. `<C,10>`) passes a null
  pointer to `atoi`. On AVR this reads whatever is at address 0, so the value is
  garbage rather than a crash (**assumed**).
- **XLC1 values above 255**: not clamped. `analogWrite` writes the low 8 bits to the
  timer on AVR, so 256 gives almost no light rather than full (**assumed**, from the AVR
  core's `analogWrite`).
- **XLC2 `<L>`**: the index isn't checked. `Adafruit_DotStar::setPixelColor` ignores
  indices past the strip (**assumed**). `<L>` also shares its brightness variable with
  `<B>`.

## State and recovery

- Nothing persists. Every output is off after a reset, including the one caused by
  opening the port.
- `pgs-scan` turns every light off after the handshake and again on disconnect.

## Hazards

- **Reflashing a board in use**: `pgs-scan` `dev` refuses 2.0.0 boards (see
  [Compatibility with `pgs-scan` `dev`](#compatibility-with-pgs-scan-dev)). Reflash a
  rig's boards only when that rig moves to this package's drivers.

## Compatibility with `pgs-scan` `dev`

Checked by reading `pgs_scan/components/lights.py` on `dev` against the 2.0.0 replies,
2026-10-29:

- **Handshake**: `XLC1` waits for exactly `EduceLab XLC1 (FW 1.1)\r\n`, and `XLC2`
  matches `EduceLab XLC2 (FW 1.\d.\d)`. Neither accepts the 2.0.0 reply, so `dev` logs
  `Invalid handshake response` and disconnects.
- **Commands**: `dev` sends only `<C,…>` to XLC1 and `<B,…>`/`<X,…>` to XLC2, all
  still valid. It never reads replies after the handshake (XLC1 discards its input
  after each write), so the `OK` lines would be ignored, as the old debug lines were.

## Version history

- **2.0.0** (both boards): every command is answered with `OK` or `ERR <reason>`, and
  the debug lines are gone. Values outside 0–255, missing or extra fields, XLC2 pixel
  indices past the strip, and over-long frames are rejected without changing anything.
  The handshake reply is now `name,hardware version,firmware version`, like MISHA's,
  and `<ID>` is accepted as well as `<?>`. XLC1 no longer accepts `<B,n>`. A `<`
  inside a frame starts a new one. The `educelab-devices` XLC drivers require this
  version.
- **XLC1 1.1, XLC2 1.2.0**: as used by `pgs-scan` `dev` (see
  [Protocol before 2.0.0](#protocol-before-200-xlc1-fw-11-xlc2-fw-120)).

## Flashing

Open `firmware/xlc1/XLC1_firmware/` or `firmware/xlc2/XLC2_firmware/` in the Arduino
IDE, or use [`arduino-cli`](https://arduino.github.io/arduino-cli/). XLC2 needs the
**Adafruit DotStar** library. Neither 2.0.0 sketch has been compiled for a board yet;
the board type is **unknown** (see [Unknowns](#unknowns)), so pick it in the IDE or pass
its FQBN:

```shell
arduino-cli lib install "Adafruit DotStar"          # XLC2 only
arduino-cli compile -b <fqbn> firmware/xlc2/XLC2_firmware
arduino-cli upload -b <fqbn> -p <port> firmware/xlc2/XLC2_firmware
```

## Unknowns

- The Arduino board and USB-serial bridge in each unit, and whether any unit has a
  unique USB serial number. Settle by plugging in each unit and recording its
  `list_ports` entry. The board type also decides the FQBN for flashing.
- The XL4's current and thermal limits at full duty.
- Whether any deployed unit runs firmware other than these two versions.

## Manual check

Not yet run. The track's final phase flashes the updated firmware on a rig and records
the results here.
