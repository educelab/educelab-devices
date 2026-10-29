# X-Carve gantry (GRBL)

The Inventables X-Carve gantry on the `pgs-scan` rigs, driven by its X-Controller, which
runs GRBL 1.1 behind an FTDI USB-serial bridge. Three linear axes, `x`, `y` and `z`,
homed against limit switches.

Driver: `educelab.devices.stages.xcarve.XCarve`. Manual check: `el-devices xcarve`.

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
  booted: `Grbl 1.1g ['$' for help]`, then `[MSG:'$H'|'$X' to unlock]`. Until homed it
  is in `Alarm` at machine position 0, 0, 0 and refuses motion. **Verified** on macOS,
  2026-10-29. The driver waits up to 5 s for the banner, and if none comes, sends a
  soft reset (`0x18`) and waits again.

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

### Settings

The driver reads `$$` on every open and never writes settings. **Verified** on the
`XCONTROLLER3HPX2Z` unit, 2026-10-29:

| Setting | Meaning | Value |
|---|---|---|
| `$10` | Status report mask | 115: includes `MPos`, which the driver needs |
| `$20`, `$21` | Soft limits, hard limits | both on |
| `$22` | Homing | on |
| `$23` | Homing direction invert mask | 3: X and Y home toward negative |
| `$24`, `$25` | Homing feed, seek rate, mm/min | 25, 750 |
| `$27` | Homing pull-off, mm | 1 |
| `$100`, `$101`, `$102` | Steps/mm, X, Y, Z | 40, 40, 188.947 |
| `$110`, `$111`, `$112` | Maximum rate, mm/min | 10000, 10000, 750 |
| `$120`, `$121`, `$122` | Acceleration, mm/s² | 500, 500, 50 |
| `$130`, `$131`, `$132` | Maximum travel, mm | 700, 700, 70 |

The driver uses `$110`–`$112`: its speed is limited to the fastest axis's rate,
166.7 mm/s on these values. Status reports must include `MPos` (bit 0 of `$10`); the
driver doesn't convert `WPos`, so a controller set to report it would make every move
time out. The reports also carry `Bf`, `WCO` and `Ov` fields, which the driver ignores.

With soft limits on, GRBL itself rejects any jog outside its travel with `error:15`, as
a second guard behind the driver's limits.

## Coordinates and limits

Positions are GRBL machine coordinates in mm. `pgs-scan` bounds them to
X −699 to 0, Y −699 to 0 and Z −45 to −1. GRBL 1.1 (`limits.c`, without
`HOMING_FORCE_SET_ORIGIN`) sets an axis that homes toward positive to minus the
pull-off, and one that homes toward negative to minus its travel plus the pull-off. With
`$23=3`, `$27=1` and `$130`–`$132` as above, homing should leave the gantry at
X −699, Y −699, Z −1, the corner of `pgs-scan`'s bounds. **Verified**: homing left it
there, 2026-10-29. So home is the minimum of X and Y and the top of Z, and every
reachable X and Y position is above home.

The driver takes its limits from the consumer and raises `PositionError` for any target
outside them, before sending anything (`pgs-scan` #71 clamped instead, then waited
forever for the stage to reach the unclamped target).

## Timing

- **Status polling**: every 0.2 s during a move, GRBL's recommended maximum of 5 Hz.
- **Arrival**: a move is done when the state is `Idle` within 0.05 mm of the target on
  every axis. Steps are coarser than GRBL's 0.001 mm reports: 0.025 mm on X and Y
  (`$100`, `$101`), about 0.005 mm on Z.
- **Move timeout**: twice the predicted time (at the stage's speed, each axis held to
  its own maximum rate, ignoring acceleration), plus 5 s. On timeout the driver sends
  jog-cancel (`pgs-scan` #67 had no timeout).
- **Homing timeout**: 120 s by default. Homing seeks at `$25` (750 mm/min), so crossing
  the full 700 mm of X or Y takes about 56 s; a slower `$25` needs a longer timeout.

## State and recovery

- **Homing** is lost on every reset: opening the port, a soft reset, or a power cycle.
  The driver requires `home()` on every connection and never homes on its own.
- **Alarms** (`ALARM:1` hard limit, `ALARM:9` homing couldn't find a switch, …) stop
  the machine and lock it. The driver loses homing, tells listeners with a `GrblAlarm`,
  and raises in any move in progress; `home()` recovers.
- **A lost connection** reaches listeners as `Disconnected`; later calls raise
  `NotConnected` until the stage is closed and reopened.
- **`$H` is answered `ok` even when homing ends in an alarm** (**verified**: after
  `ALARM:8`, 2026-10-29). The driver treats an alarm during a line as that line
  failing, whatever the reply.
- **Replies are matched by counting.** GRBL answers each line once, in order. When the
  driver gives up waiting for a line (a timeout, an alarm, `stop()`), it drops that
  line's reply when it comes, so it isn't taken for the next line's. A reset cancels
  replies still owed. Before this, the late `ok` to a failed `$H` made the next `home()`
  return at once as if homed.
- **A stuck limit switch** shows as its letter in the status report's `Pn:` field while
  nothing is moving (e.g. `Pn:Z`). With hard limits on, every jog then raises `ALARM:1`
  as soon as it starts, and homing fails with `ALARM:8`. To move the axis off the
  switch without changing settings: unplug the switch (ours are wired normally open,
  so the pin reads clear unplugged), reset, unlock with `$X`, and jog.

## Hazards

- **Opening resets the controller** and loses homing, so don't open its port to probe
  it while another program is using it.
- **`stop()` during homing soft-resets the controller**, because homing ignores
  jog-cancel. The machine stops at once, without decelerating.
- **`jog()` runs to the axis limit you gave** unless stopped. With soft limits on
  (`$20=1`), GRBL rejects a jog past its travel with `error:15`; were they off, limits
  wider than the travel would run the gantry into a hard limit switch, which alarms.
- **Z is slow**: 750 mm/min, against 10000 for X and Y. A combined move takes as long
  as its Z part needs.
- **Never home with a limit switch unplugged.** Homing seeks each axis until its switch
  triggers, so an axis without one drives into its end stop before GRBL gives up with
  `ALARM:9`. Z homes first, upward.

## Unknowns

- Whether opening the port resets the controller on Linux and Windows too.
- Whether other X-Controllers have the same settings.

## Manual check

### Last run

2026-10-29, macOS, X-Controller `XCONTROLLER3HPX2Z`, GRBL 1.1g.

- `el-devices xcarve status`: banner and alarm lock on open, settings as above, position
  0, 0, 0 in `Alarm`. No motion.
- `el-devices xcarve home`: homed Z, then X and Y, ending at X −699, Y −699, Z −1, as
  predicted. No alarms.
- `el-devices xcarve check`: all six 10 mm moves at 10 mm/s arrived within 0.05 mm.
- `pytest -m hardware` with motion allowed: the first `$H` ended in `ALARM:8` (pull-off
  failed to clear the limit switch): the **Z limit switch had broken** and read as
  triggered (`Pn:Z`) from then on. The test fixture homed again for each test, and the
  late `ok` to the failed `$H` was taken as the next one's reply; both are fixed. The
  other motion tests failed with `error:8` (not idle) and moved nothing.
- Recovery: with hard limits on, an unlocked jog alarmed at once (`ALARM:1`). With the
  switch unplugged, `Pn:Z` cleared, and Z was unlocked with `$X` and jogged down
  10 mm, to −9.998. The maintainer then reported the switch fixed.
