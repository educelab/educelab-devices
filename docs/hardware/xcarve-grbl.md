# X-Carve gantry (GRBL)

The Inventables X-Carve gantry on the `pgs-scan` rigs, driven by its X-Controller, which
runs GRBL 1.1 behind an FTDI USB-serial bridge. Three linear axes, `x`, `y` and `z`,
homed against limit switches.

Driver: `educelab.devices.stages.xcarve.XCarve`. Manual check: planned,
`el-devices xcarve`.

Unless marked otherwise, facts below are **assumed**: they come from `pgs-scan`'s
`XController` (`pgs_scan/components/motion_stages.py`, `dev`) and from GRBL 1.1's
interface docs (`doc/markdown/interface.md`, `jogging.md` in `gnea/grbl`), not from
observing the machine.

## Identity

### How the X-Controller shows up on the host

**Verified** on one X-Controller on macOS, 2026-10-29, with `pyserial-ports -v`:

| | |
|---|---|
| Port | `/dev/cu.usbserial-21320` |
| USB VID:PID | `0403:6001` (FTDI FT232R) |
| USB serial number | `XCONTROLLER3HPX2Z` |
| USB description | `X-Controller` |

The FTDI chip's memory has been programmed with that serial number and description, so
the controller can be recognized without opening its port. `XCarve.discover()` matches
VID `0403`, PID `6001` and a serial number containing `XCONTROLLER`, as `pgs-scan`
does. Whether every X-Controller's serial number starts that way is **assumed** from
`pgs-scan`; check each new unit.

- **Firmware**: `Grbl 1.1g`, from the banner `pgs-scan` expects. The driver reports
  whatever version the banner gives.

## Transport

- 115200 baud, 8N1.
- Opening the port resets the controller (DTR), and it sends its banner once it has
  booted: `Grbl 1.1g ['$' for help]`. With homing enabled it then reports
  `[MSG:'$H'|'$X' to unlock]` and refuses motion until homed. `pgs-scan` reads the
  banner right after opening, which suggests the reset; not observed directly. The
  driver waits up to 5 s for the banner, and if none comes, sends a soft reset
  (`0x18`) and waits again.

## Protocol

GRBL 1.1's own protocol; the parts the driver uses:

| Sent | Effect | Reply |
|---|---|---|
| `$$` | Lists the `$`-settings, one `$n=value` line each | `ok` |
| `$H` | Homes every axis | `ok` once homing has finished |
| `$J=G53 G21 X… Y… Z… F…` | Jogs to a machine position, in mm, at feed `F` mm/min | `ok` once queued; motion follows |
| `?` (real-time) | Asks for a status report | `<State\|MPos:x,y,z\|…>` |
| `0x85` (real-time) | Cancels jogs: decelerates and drops queued ones | none |
| `0x18` (real-time) | Soft reset: stops at once, loses homing, sends the banner | banner |

Line commands are answered `ok` or `error:N`. Real-time commands are single bytes with
no line ending, acted on as soon as they arrive, even mid-move. `ALARM:N` lines arrive
unprompted; the driver translates the codes using GRBL's docs.

Every move is a jog (`$J=`), as in `pgs-scan`, because jog-cancel stops a jog cleanly
without the feed-hold and reset that a `G1` move would need. GRBL caps each axis at its
own maximum rate (`$110`–`$112`) and slows the whole move to match.

### Settings the driver reads

| Setting | Meaning | Value per `pgs-scan` |
|---|---|---|
| `$110`, `$111` | X, Y maximum rate, mm/min | 10000 |
| `$112` | Z maximum rate, mm/min | 750 |

The driver reads these on every open and never writes settings. Its speed is limited to
the fastest axis's rate, 166.7 mm/s on these values.

Status reports must include `MPos` (`$10` with its machine-position bit set). The
driver doesn't convert `WPos`; a controller set to report it will make every move time
out. **Unknown** on our unit.

## Coordinates and limits

Positions are GRBL machine coordinates in mm. `pgs-scan` bounds them to
X −699 to 0, Y −699 to 0 and Z −45 to −1, which places home at the positive end of
each axis. After homing, GRBL with the default build reports each axis at minus the
pull-off distance (`$27`, typically 1 mm), not 0. **Unknown** on our unit; the first
rig check should record the position after `$H`.

The driver takes its limits from the consumer and raises `PositionError` for any target
outside them, before sending anything (`pgs-scan` #71 clamped instead, then waited
forever for the stage to reach the unclamped target).

## Timing

- **Status polling**: every 0.2 s during a move, GRBL's recommended maximum of 5 Hz.
- **Arrival**: a move is done when the state is `Idle` within 0.05 mm of the target on
  every axis. X-Carve steps are coarser than GRBL's 0.001 mm reports (e.g. 40 steps/mm,
  0.025 mm, on stock X and Y; **assumed**).
- **Move timeout**: twice the predicted time (at the stage's speed, each axis held to
  its own maximum rate, ignoring acceleration), plus 5 s. On timeout the driver sends
  jog-cancel (`pgs-scan` #67 had no timeout).
- **Homing timeout**: 120 s by default. Homing seeks across each axis's whole travel at
  the homing rate (`$25`), so a slow `$25` needs a longer one.

## State and recovery

- **Homing** is lost on every reset: opening the port, a soft reset, or a power cycle.
  The driver requires `home()` on every connection and never homes on its own.
- **Alarms** (`ALARM:1` hard limit, `ALARM:9` homing couldn't find a switch, …) stop
  the machine and lock it. The driver loses homing, tells listeners with a `GrblAlarm`,
  and raises in any move in progress; `home()` recovers.
- **A lost connection** reaches listeners as `Disconnected`; later calls raise
  `NotConnected` until the stage is closed and reopened.

## Hazards

- **Opening resets the controller** and loses homing, so don't open its port to probe
  it while another program is using it.
- **`stop()` during homing soft-resets the controller**, because homing ignores
  jog-cancel. The machine stops at once, without decelerating.
- **`jog()` runs to the axis limit you gave** unless stopped. Limits wider than the
  machine's travel will run it into its hard limit switch, which alarms.
- **Z is slow**: 750 mm/min, against 10000 for X and Y. A combined move takes as long
  as its Z part needs.

## Unknowns

- Whether opening the port resets the controller on every host OS.
- The position after homing, the `$27` pull-off, and `$10`'s status report mask.
- Each axis's actual travel (`$130`–`$132`), and whether soft limits (`$20`) are on.

## Manual check

Not yet run.
