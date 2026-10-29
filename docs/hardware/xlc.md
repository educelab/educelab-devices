# XLC1 and XLC2 light controllers

Lab-built LED controllers for the `pgs-scan` gantry rigs, each an Arduino-class board on
USB serial. **XLC1** drives one RGB LED strip. **XLC2** drives a white DotStar pixel
strip and the XL4 940 nm infrared array (the "XMount" LEDs). The firmware for both lives in
[`firmware/xlc1/`](../../firmware/xlc1/) and [`firmware/xlc2/`](../../firmware/xlc2/),
copied unchanged from `pgs-scan` `dev` at `aed0b5e` (2022-11-01).

Driver: planned, `educelab.devices.lights.xlc` (`XLC1`, `XLC2`). Manual check: planned,
`el-devices xlc`.

Unless marked otherwise, everything below is **assumed**: it is read from the firmware
sources and from `pgs-scan`'s `XLC1`/`XLC2` classes (`pgs_scan/components/lights.py`,
`dev`), not observed on a board.

## Identity

| | XLC1 | XLC2 |
|---|---|---|
| Handshake reply | `EduceLab XLC1 (FW 1.1)` | `EduceLab XLC2 (FW 1.2.0)` |
| Version format | `major.minor`, a literal in the reply string | `major.minor.patch`, from `VERSION_*` defines |
| Light | PLT Solutions 5050-60-IP65-RGB2450 Nano RGB strip (`XLSα` in `pgs-scan`) | Adafruit DotStar 60 LED/m, white 6000–6500 K (`XLSγ`), and EduceLab XL4 IR940 (`XL4`) |
| USB VID searched by `pgs-scan` | `0x04D9` | `0x1A86` or `0x04D9` |

The light descriptions are `pgs-scan`'s labels, not checked against the hardware. Nobody
has recorded which Arduino board or USB-serial bridge each unit has. `0x1A86` is the
generic WCH CH340 vendor ID, and `0x04D9` belongs to Holtek, so neither identifies an XLC.
The handshake is the only reliable way to identify one.

### What the firmware drives

- **XLC1**: PWM on Arduino pins 3 (red), 5 (green) and 6 (blue), through `analogWrite`.
  There is no brightness output: `<B,n>` is parsed and echoed but changes nothing.
- **XLC2**: a 222-pixel DotStar strip on hardware SPI, colour order BRG, always set to
  grey (`r = g = b`). The XL4 array is one PWM output on pin 6. The strip starts dark
  and the XL4 starts at 0.

## Transport

- 9600 baud, 8N1, through the board's USB virtual COM port.
- Opening the port resets the board (DTR). `pgs-scan` waits 2 s before sending the
  handshake. The firmware has no boot banner.
- Replies end in `\r\n` (Arduino `Serial.println`).

## Protocol (as copied: XLC1 FW 1.1, XLC2 FW 1.2.0)

Commands are framed by `<` and `>`. Anything outside a frame is ignored, and no line
ending is needed. Fields are separated by commas, and the first character of the first
field selects the command. The receive buffer holds 31 characters; extra characters
overwrite the last one.

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
one line, some send two, and an unknown command looks the same as a failure. This is
what ADR 0004 fixes; the driver needs the updated firmware (Task 1.2).

## State and recovery

- Nothing persists. Every output is off after a reset, including the one caused by
  opening the port.
- `pgs-scan` turns every light off after the handshake and again on disconnect.

## Hazards

- **Missing fields**: a command with too few fields (e.g. `<C,10>`) passes a null
  pointer to `atoi`. On AVR this reads whatever is at address 0, so the value is
  garbage rather than a crash (**assumed**).
- **XLC1 values above 255**: not clamped. `analogWrite` writes the low 8 bits to the
  timer on AVR, so 256 gives almost no light rather than full (**assumed**, from the AVR
  core's `analogWrite`).
- **XLC2 `<L>`**: the index isn't checked. `Adafruit_DotStar::setPixelColor` ignores
  indices past the strip (**assumed**). `<L>` also shares its brightness variable with
  `<B>`.
- **Unread replies**: after the handshake `pgs-scan` never reads from an XLC2, and it
  discards XLC1's input after every write, so the debug lines pile up unread or are
  thrown away. A driver that reads replies must not mistake them for answers.

## Unknowns

- The Arduino board and USB-serial bridge in each unit, and whether any unit has a
  unique USB serial number. Settle by plugging in each unit and recording its
  `list_ports` entry.
- The XL4's current and thermal limits at full duty.
- Whether any deployed unit runs firmware other than these two versions.

## Manual check

Not yet run. The track's final phase flashes the updated firmware on a rig and records
the results here.
