# MISHA LED panels

Sixteen-band LED panels used for spectral imaging, controlled over USB serial. The
board design and the original firmware come from outside the lab (firmware by Reed
Terdal, 2021); the lab owns its units and maintains the firmware in
[`firmware/misha/`](../../firmware/misha/README.md). Because the design is external,
units may differ in parts such as the USB-serial bridge. Record what each unit has.

Driver: `educelab.devices.lights.misha.MishaLight`. Manual check: `el-devices misha`.

## Identity

- **Name**: the `ID` reply's first field is `MISHA LED Panels`.
- **Hardware revision**: `1.1`, as reported in the `ID` reply. This is the
  `HW_VERSION` define compiled into the firmware, not something read from the board,
  so it's only as accurate as whoever last edited the firmware.
- **Firmware versions**: `1.0.0` (from `usb-dslr-capture-demo`) and `2.0.0` (this
  repository). The driver needs 2.0.0 or newer. See the
  [version history](../../firmware/misha/README.md#version-history).
- **Microcontroller**: an ATmega328P at 16 MHz, flashed as an Arduino Nano.
  **Assumed** from the flashing steps; not checked against a board.
- **LED drivers**: two MCP23017 I/O expanders on I²C at `0x21` and `0x20`, each pin
  switching one band on both, plus one PWM output (Arduino pin 3) that sets the
  brightness for all bands. **Assumed** from the firmware source.
- **USB-serial bridge**: Silicon Labs CP2102N, VID:PID `10C4:EA60`, with a factory
  unique 128-bit serial number. **Verified** 2026-10-13 on the panel with factory
  serial `c6f2674f…` and on `MISHA-e6a00ad8` (not recorded whether these are the
  same unit). **Unknown** whether other units use the same bridge.

### How a panel shows up on the host

| | Before naming | After naming |
|---|---|---|
| USB serial number | 32 hex digits, e.g. `c6f2674f1fc0ef1181095c04cce47fdf` | e.g. `MISHA-e6a00ad8` |
| USB description | `CP2102N USB to UART Bridge Controller` | the product string, if set |
| macOS port | e.g. `/dev/cu.usbserial-110` (named by USB location) | `/dev/cu.usbserial-MISHA_e6a00ad8` (named by serial, `-` becomes `_`) |

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

To write the name (**verified** on `MISHA-e6a00ad8`, 2026-10-13):

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
- **Brightness flickers at about 60 Hz.** The firmware drives brightness with the
  SoftPWM library, a software PWM on Timer 2 with a period of about 16.7 ms on a
  16 MHz part (`SoftPWM.cpp`, "a PWM period of approximately 60Hz (~16ms)"). Below
  100%, the LEDs blink at that rate. Exposures shorter than a period, or not a whole
  number of periods, will vary in brightness from frame to frame. **Assumed** from
  the library source; not measured on a panel. Whether 100% is steady is **unknown**.
- **UV bands.** 365, 385, and 395 nm are ultraviolet. `close()` turns everything off,
  but a crashed program can leave a band lit; the board keeps its state until it's
  told otherwise or reset.
- **Several bands at once.** The firmware lights one band at a time. Whether the
  supply can power several at once is **unknown**, so don't change that without
  measuring.
- **Locking the bridge's configuration** in Xpress Configurator is permanent.

## Unknowns

- Whether other units use the CP2102N, and what else they might use.
- How long the board actually takes to boot after a reset.
- Brightness against PWM percentage: whether it's linear.
- The actual PWM frequency and whether 100% is steady.
- Whether the supply can power more than one band at once.

## Manual check

`el-devices misha` identifies the panel, steps through every band, and turns everything
off. Results from real hardware go here.

Not yet run on hardware with firmware 2.0.0.
