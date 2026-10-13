# MISHA LED panels

Sixteen-band LED panels used for spectral imaging, controlled over USB serial. The
board design and the original firmware come from outside the lab: the boards are by
Carlos Barrios at RIT's Chester F. Carlson Center for Imaging Science (schematic
rev 1.1, 2021-08-28) and the firmware by Reed Terdal (2021). The lab owns its units and
maintains the firmware in [`firmware/misha/`](../../firmware/misha/README.md). Because
the design is external, units may differ in parts such as the USB-serial bridge.
Record what each unit has.

The schematic (`2021_08_28_V1_1_MISHA_PCBWay_PCBWay Community.pdf`) and the bill of
materials with the LED design sheet (`MISHA Public BOM_PCBWay Community.xlsx`) are in
the lab's OneDrive (EduceLab - General › FLEX › Photo Lab Outfitting › MISHA Spectral
System). Facts below marked "from the schematic" or "from the BOM" come from them, not
from inspecting a board.

Driver: `educelab.devices.lights.misha.MishaLight`. Manual check: `el-devices misha`.

## Identity

- **Name**: the `ID` reply's first field is `MISHA LED Panels`.
- **Hardware revision**: `1.1`, as reported in the `ID` reply. This is the
  `HW_VERSION` define compiled into the firmware, not something read from the board,
  so it's only as accurate as whoever last edited the firmware.
- **Firmware versions**: `1.0.0` (from `usb-dslr-capture-demo`) and `2.0.0` (this
  repository). The driver needs 2.0.0 or newer. See the
  [version history](../../firmware/misha/README.md#version-history).
- **Two boards per panel**, joined by a cable (RJ45 or M12) that carries 9 V, 5 V,
  I²C, and the PWM line. The main board has the Arduino and the power input (9 V, 4 A,
  36 W supply). From the schematic.
- **Microcontroller**: a plug-in Arduino Nano module (part 102010268, an ATmega328P
  at 16 MHz) on the main board. From the schematic. Its USB bridge is the CP2102N
  below.
- **The second board** is the same PCB without the Arduino: the BOM populates the
  drivers and their capacitors on "Both" panels and the Arduino header on "Main" only.
  From the BOM.
- **Band switching**: an MCP23017 I/O expander at I²C `0x21` (its `A0` is driven high by
  Arduino pin 4, and pulled low by 10 kΩ otherwise, which would put the second board's
  at `0x20`). Each output pin is one band's enable, pulled down by 10 kΩ, so bands are
  off while the expander is in reset. From the schematic and the firmware.
- **LED drivers**: one ST STCS2A linear constant-current driver per band. Each sets its
  current with a 0.2 Ω sense resistor against a 100 mV reference, **0.5 A per
  driver** (the design sheet lists 500 mA for every band), and has a 22 nF slope
  capacitor, C5 (see [Timing](#timing)). The BOM fits C5 on all 32 drivers (Yageo
  AC1206KRX7R0BB223); that ours have them is **assumed** until someone looks for a
  capacitor at pin 9 of a driver. From the schematic, the BOM, and the STCS2A datasheet
  (Rev 3, July 2008). The STCS2 without the "A" has no slope pin and a 4 µs ramp; its
  datasheet doesn't apply here.
- **LEDs per band, per board**: one LED star for 365–420 nm, two for 450–850 nm, and
  three for 940 nm, in series on the band's driver. From the BOM design sheet.
- **Brightness**: Arduino pin D3 drives every driver's PWM input, and goes to the
  second board through the cable. It's pulled up by 10 kΩ, but the enables are pulled
  down, so a resetting Arduino doesn't light anything. From the schematic.
- **USB-serial bridge**: Silicon Labs CP2102N, VID:PID `10C4:EA60`, with a factory
  unique 128-bit serial number. **Verified** on two units, 2026-10-13:
  `MISHA-e6a00ad8` and `MISHA-2108d7a8` (one of them had factory serial
  `c6f2674f…`). Units come from an external design, so check each new one.

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
| `RESET_SETTLE_S` | 2.0 s | Opening the port resets the board (DTR), and it ignores commands until its bootloader hands over. **Assumed**, from the spectral prototype's constant; to be measured. |
| `HANDSHAKE_TIMEOUT_S` | 2.0 s | Wait for the `ID` reply after the settle. Also from the prototype. |
| `REPLY_TIMEOUT_S` | 1.0 s | Wait for `OK`/`ERR`. A reply line takes about 10 ms to send at 9600 baud; the board's own processing time is **unknown** but should be far shorter. |

Opening a panel therefore takes about 2 seconds.

**LED current ramps over about 1.8 ms.** Each STCS2A ramps its current on and off at a
rate set by its slope capacitor: 800 µs at 10 nF, and linear in the capacitance
(datasheet, "Rise/Fall time of the current on PWM transition"). With the boards' 22 nF
that's about 1.8 ms each way. A band reaches full brightness about 2 ms after `OK`,
plus the time the reply takes to arrive, so allow at least that before exposing.
**Assumed** from the datasheet; to be measured.

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

## Hazards

- **Opening the port resets the board.** The DTR line pulses when a port opens, which
  resets an Arduino-style board. Asking pyserial to keep DTR low doesn't reliably stop
  it on Linux or macOS. Any tool that opens ports to see what's there can reset
  *other* boards, e.g. an X-Carve's GRBL controller, which then loses its homing. So
  the driver never opens a port it hasn't matched from USB descriptors or been given
  explicitly.
- **Brightness flickers at about 60 Hz below 100%.** The firmware drives D3 with the
  SoftPWM library: a software PWM on Timer 2 with a period of about 16.7 ms on a
  16 MHz part (`SoftPWM.cpp`, "a PWM period of approximately 60Hz (~16ms)"). Because
  the drivers ramp over about 1.8 ms, each pulse of light is a trapezoid. Exposures
  shorter than a period, or not a whole number of periods, vary in brightness from
  frame to frame. Below roughly 10% the pulses are shorter than a ramp, so brightness
  falls off faster than the percentage. At 100% SoftPWM switches off for 1/256 of
  each period (about 65 µs), which the 1.8 ms ramp smooths to a dip of a few percent;
  light is close to steady. **Assumed** from the library source and the datasheet;
  to be measured. For imaging, light the band at 100% and set exposure with the
  camera.
- **Don't raise the PWM frequency without changing the slope capacitors.** With
  1.8 ms ramps, a period near or below 4 ms (above about 250 Hz) never lets the
  current settle: it climbs to full above 50% duty and falls to zero below it, so
  dimming stops working. Faster PWM needs smaller slope capacitors on every driver
  (the datasheet gives about 4 µs with none, at the cost of more EMI), which is a
  hardware change to an external design. If our boards turn out not to have C5
  fitted, despite the BOM, this hazard doesn't apply and the PWM can go much faster.
- **Power.** A lit band draws about 1 A (0.5 A on each board) from a 9 V, 4 A supply.
  The firmware lights one band at a time; four bands would reach the supply's limit.
  From the schematic and the BOM.
- **UV bands.** 365, 385, and 395 nm are ultraviolet. `close()` turns everything off,
  but a crashed program can leave a band lit; the board keeps its state until it's
  told otherwise or reset.
- **Locking the bridge's configuration** in Xpress Configurator is permanent.

## Unknowns

- Whether units beyond the two checked use the CP2102N, and what else they might use.
- How long the board actually takes to boot after a reset.
- The actual current ramp time, and that C5 is fitted as the BOM says.
- The actual PWM frequency and how steady 100% is.
- Brightness against PWM percentage, especially below 10%.

## Manual check

`el-devices misha` identifies the panel, steps through every band, and turns everything
off. Results from real hardware go here.

Not yet run on hardware with firmware 2.0.0.
