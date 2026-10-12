# Lab firmware acknowledges every command

Firmware for lab-built controllers (MISHA, XLC1, XLC2, and future boards) answers every command with exactly one line, `OK` or `ERR <reason>`, and reports its name and version in a handshake. Drivers wait for that reply instead of guessing whether one is coming, and refuse firmware older than the version they were written for, pointing to the reflash steps. We control this firmware, so we fixed the protocol rather than encoding each board's quirks (silent failures, debug prints that look like replies) into its driver.

## Consequences

- Updating a board's firmware is part of adopting its new driver. Boards still driven by older application code (e.g. XLC boards under `pgs-scan` `dev`) are reflashed together with that application's migration.
- Firmware sources live in `firmware/` next to the drivers, so a protocol change and its driver change land together.
