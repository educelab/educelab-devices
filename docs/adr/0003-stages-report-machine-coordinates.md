# Stages report machine coordinates; origins belong to consumers

Stages report and accept positions only in the device's own (machine) coordinates. User-pinned origins and UI coordinate remaps live in consumers, even though the original Foldio360 client offered `set_origin()` and a `home()` that meant "move to origin". The Foldio's position counter silently resets at power-on, so any stored origin can go stale undetectably; keeping origins out of the library means the code that pins an origin is the code that owns that hazard and its warning.

## Considered Options

- **A generic work origin on every stage** (as in GRBL's G54): revisit if several consumers end up writing the same offset logic.
- **A Foldio-only origin**: makes one stage's positions mean something different from every other stage's.
