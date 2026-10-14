# MISHA LED panels

Sixteen-band LED panels used for spectral imaging, controlled over USB serial. The
board design (Carlos Barrios, RIT, schematic rev 1.1, 2021) and the original firmware
(Reed Terdal, 2021) come from outside the lab; the lab owns its units and maintains
the firmware in [`firmware/misha/`](../../firmware/misha/README.md). Because the design
is external, units may differ in parts such as the USB-serial bridge. Record what each
unit has.

Board facts below come from the schematic and bill of materials in the lab's OneDrive
(EduceLab - General › FLEX › Photo Lab Outfitting › MISHA Spectral System), not from
inspecting a board.

Driver: `educelab.devices.lights.misha.MishaLight`. Manual check: `el-devices misha`.

## Identity

- **Name**: the `ID` reply's first field is `MISHA LED Panels`.
- **Hardware revision**: `1.1`, as reported in the `ID` reply. This is the
  `HW_VERSION` define compiled into the firmware, not something read from the board,
  so it's only as accurate as whoever last edited the firmware.
- **Firmware versions**: `1.0.0` (from `usb-dslr-capture-demo`) and `2.0.0` (this
  repository). The driver needs 2.0.0 or newer. See the
  [version history](../../firmware/misha/README.md#version-history).
- **USB-serial bridge**: Silicon Labs CP2102N, VID:PID `10C4:EA60`, with a factory
  unique 128-bit serial number. **Verified** on two units, 2026-10-13:
  `MISHA-e6a00ad8` and `MISHA-2108d7a8` (one of them had factory serial
  `c6f2674f…`). Units come from an external design, so check each new one.

### What the firmware drives

- **Two boards per panel**: the main board carries an Arduino Nano module (ATmega328P,
  16 MHz); the second is the same PCB without it, reached over a cable that carries
  I²C and the brightness line.
- **Band enables**: one MCP23017 I/O expander per board, at I²C `0x21` on the main
  board (Arduino pin 4 drives its `A0` high) and `0x20` on the second. Arduino pin 7
  resets the main board's; the cable doesn't carry it to the second. Each expander output enables one band; the firmware maps wavelengths to
  pins. The enables are pulled down, so every band is off while the expanders are in
  reset.
- **Brightness**: Arduino pin D3 is one PWM line to every band's LED driver on both
  boards. The drivers (ST STCS2A) ramp their current on and off over about 1.8 ms,
  which limits how the firmware can dim (see
  [Brightness and exposure](#brightness-and-exposure)).
- **One band at a time**: all bands share the one brightness line, and each lit band
  draws about 1 A from the panel's 9 V, 4 A supply.

### How a panel shows up on the host

| | Before naming | After naming |
|---|---|---|
| USB serial number | 32 hex digits, e.g. `c6f2674f1fc0ef1181095c04cce47fdf` | e.g. `MISHA-2108d7a8` |
| USB description | `CP2102N USB to UART Bridge Controller` | the product string, e.g. `MISHA LED Panels` (**verified** on macOS; on Linux **assumed**; Windows shows its driver's name instead, **assumed**) |
| macOS port | e.g. `/dev/cu.usbserial-110` (named by USB location) | `/dev/cu.usbserial-MISHA_2108d7a8` (named by serial, `-` becomes `_`) |

The macOS port names are **verified**, 2026-10-13. On Linux the port
is `/dev/ttyUSB<n>`, and Windows gives a renamed device a new COM port number
(**assumed**, from how those systems name CP210x ports).

## Naming a panel

`MishaLight.discover()` and `MishaLight()` without arguments only recognize a panel
whose USB serial number contains `MISHA`, because recognizing it any other way means
opening its port (see [Hazards](#hazards)). Naming is optional: an unnamed panel still
opens with `MishaLight(serial_number="<factory serial>")` or `MishaLight("<port>")`.

Names are `MISHA-` and 8 random hex digits, so nobody has to keep a sequence:

```shell
python3 -c "import secrets; print('MISHA-' + secrets.token_hex(4))"
```

To write the name (**verified** on `MISHA-e6a00ad8` and `MISHA-2108d7a8`, 2026-10-13; rewriting an already-programmed configuration is verified on `MISHA-2108d7a8`):

1. Install **Simplicity Studio 5**. Studio 6 doesn't support CP210x devices or Xpress
   Configurator (Silicon Labs community answer, 2026-10-13).
2. Plug in the panel and open **Xpress Configurator** from Compatible Tools.
3. Set the **Serial String** to the name. Optionally set the Product String to
   `MISHA LED Panels`.
4. Leave the VID and PID as they are. The driver and the operating systems' CP210x
   drivers rely on `10C4:EA60`.
5. **Don't lock the configuration.** The lock is permanent; unlocked, the name can be
   rewritten (the CP2102N's configuration memory is rated for at least 20,000 writes,
   per its datasheet, Rev 1.3, §3.1.4).
6. Program the chip, unplug and replug the panel, and check that
   `el-devices discover` lists it.

The name lives in the bridge chip, so it stays with the board through power cycles and
firmware reflashes. Write the name on the panel's housing too.

## Transport

- USB serial at 9600 baud, 8N1 (the firmware's `Serial.begin(9600)`).
- ASCII lines ending in `\n`. The firmware trims surrounding whitespace, so `\r\n`
  also works.
- A command without a newline is still processed after about 1 second, when the
  firmware's `readStringUntil` times out (**assumed**, from Arduino's default stream
  timeout).

## Protocol

Firmware 2.0.0 follows [ADR 0004](../adr/0004-lab-firmware-acknowledges-every-command.md):
every command gets exactly one reply line, and a rejected command changes nothing.

| Command | Effect | Reply |
|---|---|---|
| `ID` or `?` | Identifies the board. | `<name>,<hardware>,<firmware>`, e.g. `MISHA LED Panels,1.1,2.0.0` |
| `STATE` | Reports the lit wavelength and its intensity. | `OK <nm>,<percent>`, or `OK 0,0` if all are off |
| `<nm>,<percent>` | Lights `<nm>` at `<percent>` (0–100) and turns off the others. If `<nm>` is already lit, only its intensity changes. | `OK` |
| `<nm>,0` | Turns off `<nm>` if it's lit; a different lit band stays on. | `OK` |
| `OFF`, or `0,<percent>` | Turns everything off. `0,0` is how 1.0.0 hosts do it. | `OK` |

Errors: `ERR unknown wavelength`, `ERR intensity out of range` (above 100), and
`ERR invalid command` (anything else, including trailing characters such as
`365,50x`). Blank lines are ignored and get no reply.

Wavelengths (nm): 365, 385, 395, 420, 450, 470, 500, 530, 560, 590, 615, 630, 660, 730,
850, 940. The driver's channel IDs are these with `nm` appended, e.g. `"450nm"`.

Firmware 1.0.0 replied only to `ID` and to malformed commands (`Invalid command`), and
an unknown wavelength silently turned everything off. The driver refuses it.

## Timing

| Constant | Value | Why |
|---|---|---|
| `RESET_SETTLE_S` | 2.0 s | Opening the port resets the board (DTR), and it ignores commands until its bootloader hands over. **Verified**: ready about 1.67 s after the port opens. |
| `HANDSHAKE_TIMEOUT_S` | 2.0 s | Wait for the `ID` reply after the settle. `ID` takes about 36 ms. |
| `REPLY_TIMEOUT_S` | 1.0 s | Wait for `OK`/`ERR`. `STATE` takes about 19 ms, nearly all of it the reply crossing at 9600 baud (about 1 ms per character). |

Opening a panel therefore takes about 2 seconds.

**Measured** 2026-10-14 on `MISHA-e6a00ad8`, firmware 2.0.0, from macOS. A probe
opened the port, waited a set delay, and sent `ID` once, three times for each delay in
50 ms steps. Nothing answered at 1.55 s or less; everything answered from 1.60 s, and
the slower replies at 1.60 s and 1.65 s put the board ready at about 1.665 s. Most of
that is the bootloader: `setup()` itself has only about 20 ms of delays. Opening the
port took 5–115 ms more. Round trips over 20 tries: `ID` 36 ms median (max 37 ms),
`STATE` 19 ms (max 21 ms). Other units may boot at a different speed.

## State and recovery

- **Power-up and reset**: every band off (the firmware's `setup()`). Any reconnect
  that resets the board (most do; see Hazards) leaves it dark.
- **`STATE`** reports what the firmware last commanded, not what the LEDs are doing.
  A failed LED or a loose cable still reads as lit.
- **Driver**: `open()` reads `STATE` rather than changing anything, so reconnecting to a
  board that didn't reset leaves its band on. `level()` asks the panel every time.
  `close()` sends `OFF`; if the panel was unplugged, it logs a warning and closes
  anyway.
- **A reply that arrives after its timeout**: the driver discards unread input before
  each command, so a late `OK` can't be taken as the next command's reply.

## Brightness and exposure

**Run bands at 100% and set exposure with the camera.** Below 100% the light flickers,
so dimming a band changes brightness from frame to frame unless the exposure is long
enough. To trim exposure against a reference such as Spectralon, keep to these limits:

| Level | Exposure that stays within ~1% frame to frame |
|---|---|
| 95–100% | any tested (1/60 s to 1 s) |
| 90% | 1/15 s or longer |
| 25–80% | 1/4 s or longer |
| 10% | 1 s or longer |
| 5% | avoid: about half as bright as set |

At a looser 3%: 90% and up at any tested speed except 1/40 s, 80% at 1/15 s or longer,
and 50–70% at 1/8 s or longer. Exposures shorter than 1/60 s weren't tested; flicker
grows as exposure shortens, so expect even 95% to vary by a few percent at 1/125 s.

**Measured** 2026-10-14 at 530 nm on one panel running firmware 1.0.0 (the PWM code is
unchanged in 2.0.0), with a Nikon Z 7II photographing Spectralon: 10 RAW frames per
level and shutter speed, comparing the frame-to-frame spread of the mean against the
~1% spread at 100%. With 10 frames, differences under about 1% are within noise.

- **100% is steady.** Its spread was 0.7–1.4% at every shutter speed, with no rise at
  1/40 s, where flicker shows most. Any flicker at 100% is below that.
- **Dimming uses a ~60 Hz software PWM.** The firmware drives D3 with the SoftPWM
  library: Timer 2, a period of about 16.7 ms on a 16 MHz part (`SoftPWM.cpp`). Below
  100% the light blinks at that rate, so exposures that aren't a whole number of
  periods vary in brightness. Flicker was worst at 1/40 s (about 1.5 periods): 18% at
  50%, 2.5% at 90%. The results fit ~60 Hz; the frequency wasn't measured directly.
- **No shutter speed reliably avoids it.** 1/60, 1/30, and 1/15 s are close to whole
  periods at their nominal lengths, yet flickered by up to 7% between 50% and 80%. The
  camera's actual exposure times differ from the labels (cameras commonly time 1/60 s
  as 1/64 s), so don't rely on matching the period.
- **Brightness tracks the level from 25% to 100%**, within about 3% (at 1 s: 50% gave
  51.4%, 90% gave 92.1%, 95% gave 97.8%). Below that it falls off: 10% gave 8.4% and 5%
  gave 2.1%.
- **The LED drivers ramp over about 1.8 ms**, which explains the fall-off. Each
  driver's 22 nF slope capacitor sets its rise and fall time: 800 µs at 10 nF, linear in
  the capacitance (STCS2A datasheet, Rev 3). So each pulse of light is a trapezoid, and
  at low levels the pulses are shorter than a ramp. A model of a 60 Hz PWM with 1.8 ms
  ramps predicted 0.47× at 5% (measured 0.42×) and roughly bounds the flicker above.
  A newly lit band reaches full brightness about 2 ms after `OK` (**assumed**).
- **Faster PWM won't fix dimming on these boards.** With 1.8 ms ramps, a period below
  about 4 ms (above about 250 Hz) never lets the current settle: it climbs to full
  above 50% duty and falls to zero below it. Faster dimming would need smaller slope
  capacitors on every driver, a change to the boards.

## Hazards

- **Opening the port resets the board.** The DTR line pulses when a port opens, which
  resets an Arduino-style board. Asking pyserial to keep DTR low doesn't reliably stop
  it on Linux or macOS. Any tool that opens ports to see what's there can reset
  *other* boards, e.g. an X-Carve's GRBL controller, which then loses its homing. So
  the driver never opens a port it hasn't matched from USB descriptors or been given
  explicitly.
- **UV bands.** 365, 385, and 395 nm are ultraviolet. `close()` turns everything off,
  but a crashed program can leave a band lit; the board keeps its state until it's
  told otherwise or reset.
- **Locking the bridge's configuration** in Xpress Configurator is permanent.

## Unknowns

- Whether units beyond the two checked use the CP2102N, and what else they might use.
- Boot time on units other than `MISHA-e6a00ad8`.
- The exact PWM frequency and current ramp time, measured directly.
- Whether other bands, panels, and cameras flicker the same way; only 530 nm on one
  panel with one camera was measured.
- Where the ~1% frame-to-frame spread at 100% comes from (camera shutter or light).

## Manual check

`el-devices misha` identifies the panel, steps through every band, and turns everything
off. Results from real hardware go here.

Not yet run on hardware with firmware 2.0.0.
