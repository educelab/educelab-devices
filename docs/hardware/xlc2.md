# XLC2 light controller

A lab-built LED controller for the `pgs-scan` gantry rigs: an Arduino-class board on
USB serial that drives a white DotStar pixel strip and the XL4 940 nm infrared array
(the "XMount" LEDs). Its firmware lives in [`firmware/xlc2/`](../../firmware/xlc2/). It
was copied from `pgs-scan` `dev` at `aed0b5e` (2022-11-01) and updated to 2.0.0 and
2.1.0 here (see [Version history](#version-history)).

**XLC1**, an earlier controller for one RGB strip, is a separate device rather than an
older revision of this board. Its units have been retired (maintainer, 2026-10-29), so
this package has no XLC1 driver or firmware. Its 1.1 firmware is in `pgs-scan` and in
this repository's history (commit `3f85a42`).

Driver: `educelab.devices.lights.xlc.XLC2`. Manual check: `el-devices xlc`.

Nothing in the USB descriptors identifies an XLC2 or tells two apart (see
[Identity](#identity)), so from firmware 2.1.0 each board stores a **unit ID** in EEPROM
and reports it in the handshake. The driver reports it as `info.serial_number`.

- `discover()` lists the ports with a USB-serial bridge seen on XLC2s, without opening
  them, so their serial numbers are unknown. Other boards with the same bridge are
  listed too.
- `XLC2.probe()` sends those ports the handshake and reports each XLC2's unit ID and
  firmware. Opening a port resets the board behind it, so probing is limited to make
  that safe; see [Finding boards](#finding-boards).
- A board is opened by `port=`, or by its unit ID with `serial_number=`, which probes.

Unless marked otherwise, everything below is **assumed**: it is read from the firmware
source and from `pgs-scan`'s `XLC2` class (`pgs_scan/components/lights.py`, `dev`), not
observed on a board. The 2.0.0 replies were checked by compiling the sketch on a host
against stubbed Arduino libraries, not on a board.

## Identity

- **Handshake reply**: `EduceLab XLC2,1.0,2.1.0,<unit ID>` from 2.1.0, with the unit ID
  empty until one is set; `EduceLab XLC2,1.0,2.0.0` in 2.0.0; `EduceLab XLC2 (FW 1.2.0)`
  before.
- **Unit ID**: 1–16 characters from `A–Z`, `a–z`, `0–9`, `_` and `-`, stored in the
  Nano's EEPROM by `<U,id>` (see [Setting a unit ID](#setting-a-unit-id)). The lab
  labels units `XLC2-NN`; nothing enforces it.
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
apart from another XLC2, or from any other HT42B534, by its descriptors. Unlike MISHA's
CP2102N, its descriptors can't be rewritten on this board (maintainer, issue #37,
2026-11), which is why the unit ID lives in the firmware's EEPROM instead. The `usbmodem` name suggests
the bridge is a USB CDC device, which macOS names from the serial number (**assumed**).
Other units may have other bridges, since `pgs-scan` also searches for the CH340's VID.

On Windows 11 (2026-11-02, the same board as COM4) it has the same VID:PID and serial
number. Windows binds Microsoft's in-box CDC driver, so pyserial reports the description
`USB Serial Device (COM4)` and the manufacturer `Microsoft`. **Verified**.

### What the firmware drives

- A 222-pixel DotStar strip on hardware SPI, colour order BRG, always set to grey
  (`r = g = b`).
- The XL4 array, one PWM output on pin 6.
- Both start off at power-up.
- **Lit together**: the strip and the array have separate outputs, so `XLC2` declares
  `concurrency = ChannelConcurrency.INDEPENDENT` (**assumed** until the manual check
  below confirms both stay lit together).
- **Flashes on reset**: the strip flashes briefly at some point when the port opens
  (**verified**, observed by the maintainer, 2026-10-29). The likely cause is the Nano's
  bootloader blinking its LED on D13, which is also the SPI clock to the strip, so stray
  bits reach the strip until `setup()` clears it (**assumed**). Harmless, but a camera
  exposing while the port opens could catch it.

### Finding boards

Opening a port resets the board behind it (DTR), which loses an X-Carve's homing and
turns an XLC2's lights off. `discover()` never opens a port. `XLC2.probe()`, and
`XLC2(serial_number=...)`, which uses it, open a port only if:

- its VID:PID is a bridge seen on XLC2s: `04D9:B534` (Holtek HT42B534) or `1A86:7523`
  (WCH CH340, which `pgs-scan` searches for; **assumed**, no CH340 unit checked yet);
- it opens. Every serial port is opened exclusively, so a port another program holds
  is skipped. On Windows that's any program. On Linux and macOS pyserial takes an
  advisory `flock`, which only stops programs that also lock the port (this package,
  or pyserial with `exclusive=True`); `screen`, `minicom` or a plain pyserial script
  don't, so their boards can still be reset. Close them before probing.

The allowlist must never include another driver's ports: the X-Controller is an FTDI
`0403:6001` and MISHA a CP2102N `10C4:EA60`. A test checks that the bridges' vendor IDs
don't overlap.

Candidates are probed at once, so a probe takes about 4 s (2 s reset settle, up to 2 s
for the handshake) however many there are. A port that doesn't answer with an XLC2
handshake is skipped and logged at debug level. `XLC2(serial_number=...)` keeps the
matching board's port open from the probe, so that board is reset once, not twice.

## Transport

- 9600 baud, 8N1, through the board's USB virtual COM port.
- Opening the port resets the board (DTR). `pgs-scan` waits 2 s before sending the
  handshake. The firmware has no boot banner.
- Replies end in `\r\n` (Arduino `Serial.println`).
- **On Windows, changing the port's timeout while a reply is arriving kills the
  link.** **Verified** on Windows 11 with pyserial 3.5, 2026-10-30 and 2026-11-02:
  - Setting `Serial.timeout` just after sending `<?>` cut the reply to a few bytes
    plus garbage (`Edu\x16%`, `du\x00\x16%`). After that the same handle got nothing
    for at least 6 s.
  - DTR and RTS stayed asserted and the port's settings were unchanged. The board
    didn't reset, since it boots in under 2 s and never answered again.
  - Closing and reopening the port brought it straight back.
  - Changing the timeout while nothing was arriving was harmless.

  pyserial applies a timeout change on Windows by reconfiguring the whole port
  (**assumed**). Which part stays wedged is **unknown**: the in-box CDC driver, the
  HT42B534 or the firmware. So the transport opens each port with a fixed 50 ms read
  timeout and never changes it; `read_line` polls up to its own deadline. Library
  0.3.0 changed the timeout right after sending the handshake, so it never found an
  XLC2 on Windows.

## Protocol (2.1.0)

Commands are framed by `<` and `>`. Anything outside a frame is ignored, so a trailing
line ending is harmless. A `<` inside a frame starts a new frame, dropping the
unfinished one. Fields are separated by commas, and spaces around a number are allowed.
A frame longer than 31 characters is rejected.

`<?>` or `<ID>` (any case) is the handshake. The reply is one line,
`name,hardware version,firmware version,unit ID`. The first three fields are the same
shape as MISHA's, and the unit ID is empty until one is set (ADR 0004):

```
> <?>
< EduceLab XLC2,1.0,2.1.0,XLC2-01
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
| `<U,id>` | Stores unit ID `id` in EEPROM (2.1.0) |

The driver uses `<B>` for channel `white` and `<X>` for channel `940nm`. These channel
IDs are stable: consumers store them in presets and metadata (`pgs-scan` does, keeping
its own display labels `XLSγ` and `XL4`), so renaming one is a breaking change. A level
is the duty divided by 255, and every whole duty from 0 to 255 survives the round trip
through a level exactly, which a test pins.

| Reply | Cause |
|---|---|
| `ERR unknown command` | The command letter isn't one of these, or the frame is empty |
| `ERR invalid command` | Wrong number of fields, a value that isn't a number from 0 to 255, a unit ID that isn't 1–16 allowed characters, or a frame that's too long |
| `ERR index out of range` | `<L>` with an index past the strip |

2.0.0 is the same, except that its handshake has no fourth field and `<U>` gets
`ERR unknown command`.

### Setting a unit ID

```shell
el-devices xlc --port /dev/cu.usbmodem00001 --set-id XLC2-01
```

or `XLC2.set_unit_id("XLC2-01")` on an open board. `el-devices xlc --set-id` with no
port probes, and refuses unless exactly one XLC2 answers. The ID is written to EEPROM
as a magic byte (`0xEC`), a length, then the characters; blank EEPROM (`0xFF`) has no
magic byte, so it reads as unset. The magic byte is cleared before the characters are
written and set last, so a write cut short reads as unset rather than garbled.
Uploading a sketch through the Nano's bootloader leaves EEPROM alone, so the ID survives
reflashing from the Arduino IDE, and power cycles (**verified**, 2026-11-18). A board
that has never had an ID reports an empty field (**verified** on one board).

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
  It turns both outputs off on open and close, as `pgs-scan` does, sending each off
  command even if the other fails, so a missed reply can't leave the 940 nm array lit.

## Hazards

- **Probing resets other boards on the same bridge**: `XLC2.probe()`,
  `XLC2(serial_number=...)` and `el-devices xlc` without `--port` open every port with
  an XLC2's bridge that isn't locked (see [Finding boards](#finding-boards)). Any other
  CH340 or HT42B534 board on the host is reset too. The X-Controller and MISHA panels
  aren't, being on other bridges.
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

- **2.1.0**: a unit ID, stored in EEPROM by `<U,id>` and reported as a fourth handshake
  field, empty until set. Drivers for 2.0.0 that split the reply into exactly three
  fields don't recognize it.
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
**Adafruit DotStar** library. The board is an **Arduino Nano** (maintainer, 2026-10-29),
and 2.0.0 was flashed to it from the Arduino IDE. With `arduino-cli` (untried):

```shell
arduino-cli lib install "Adafruit DotStar"
arduino-cli compile -b arduino:avr:nano firmware/xlc2/XLC2_firmware
arduino-cli upload -b arduino:avr:nano -p <port> firmware/xlc2/XLC2_firmware
```

Nanos with the old bootloader need `arduino:avr:nano:cpu=atmega328old` instead. Which
bootloader this one has is **unknown**.

## Unknowns

- The USB-serial bridge in the units not yet checked, and whether they're all Nanos.
  Settle by plugging in each unit and recording its `list_ports` entry.
- Whether CH340 units use PID `7523`, the only CH340 PID probed.
- Whether probing leaves a homed X-Carve homed. It should, since its FTDI bridge isn't
  on the allowlist, but it hasn't been tried with both attached.
- Whether exclusive open behaves on Windows and Linux as on macOS.
- The XL4's current and thermal limits at full duty.
- Whether any deployed unit runs firmware other than 1.2.0.
- What a mid-reply timeout change on Windows wedges: the CDC driver, the HT42B534 or
  the firmware. Reopening the port both resets the board and reconfigures the port,
  so the probes so far can't tell. It doesn't matter while the transport never
  changes the timeout.

## Manual check

### Still owed

Tracked in issue #39.

- `el-devices xlc` with no port while the X-Carve is attached and homed: it must stay
  homed.
- Exclusive open on Windows (and Linux, if a rig is available), and for MISHA and
  X-Carve ports.
- The VID:PID of any unit with a CH340.
- White and 940 nm lit together: `test_channels_stay_lit_together` in
  `tests/test_xlc_hardware.py`, watching that both stay on (backs the `INDEPENDENT`
  declaration).

### Last run

2026-11-18, macOS 27.2, Python 3.12, pyserial 3.5, the HT42B534 board at
`/dev/cu.usbmodem00001` (serial `0000`), the only USB-serial device attached. Branch
`xlc2-unit-id`.

On 2.0.0, before reflashing:

- `XLC2.discover()` listed the port without opening it; `XLC2.probe()` found the board
  (firmware 2.0.0, no unit ID) in 2.0 s.
- `el-devices xlc` with no port probed, found the board, and lit and cleared both
  channels.
- `pytest -m hardware tests/test_xlc_hardware.py`: 9 passed, 2 skipped (unit ID).
- **Exclusive open**: with the port held by one process, `el-devices xlc --port`
  in another failed with `Could not exclusively lock port … [Errno 35] Resource
  temporarily unavailable`, without resetting the board; a probe skipped the port. Once
  the first process closed it, the port opened normally.

Flashed 2.1.0 from the Arduino IDE (maintainer):

- `el-devices xlc` reported `(no ID)`; `--set-id XLC2-01` was acknowledged, and
  `--serial-number XLC2-01` then found and opened the board by probing.
  `--serial-number XLC2-02` failed with `no XLC2 has unit ID 'XLC2-02'; found XLC2-01
  at /dev/cu.usbmodem00001`.
- `XLC2_UNIT_ID=XLC2-01 pytest -m hardware tests/test_xlc_hardware.py`: 11 passed.
- The ID was still `XLC2-01` after a power cycle (unplug and replug; same port name)
  and after reflashing 2.1.0 from the Arduino IDE.

### Run of 2026-11-02

2026-11-02, Windows 11, Python 3.12, pyserial 3.5, the same board at COM4. This was the
`fix/serial-fixed-timeout` branch (0.3.1), which never changes the port's timeout.

- `el-devices xlc`: found `XLC2 at COM4, firmware 2.0.0, hardware 1.0`, then set white
  and 940 nm to 25% and turned all off, with no errors. On 0.3.0 the same check failed
  with `DeviceNotFound` (see [Transport](#transport)).

### Run of 2026-10-29

2026-10-29, macOS, the XLC2 at `/dev/cu.usbmodem00001` (HT42B534 bridge, serial `0000`),
flashed with 2.0.0 from the Arduino IDE by the maintainer. Its port kept the same name
after flashing.

- `el-devices xlc`: handshake `EduceLab XLC2,1.0,2.0.0`; `<B,64>`, `<X,64>` and the
  zeros each answered with one `OK`.
- `pytest -m hardware tests/test_xlc_hardware.py` with `XLC2_PORT` set: 8 passed. Each
  channel and all-off were acknowledged, and `<B,256>`, `<B>`, `<L,222,9>` and `<Q>`
  were rejected with the documented reasons. **Verified**: the 2.0.0 protocol above.
