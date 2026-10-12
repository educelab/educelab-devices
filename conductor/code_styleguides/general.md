# General Style Guide

## Readability

- Write for humans first. Prefer clarity over cleverness.
- Keep functions focused on one task; return early instead of nesting.
- Use the vocabulary in `CONTEXT.md` for names (e.g. `Channel`, not `led`; `Stage`, not `motor`).

## Comments and docstrings

- Comments are need-to-know: explain *why*, never restate *what*. Inline comment blocks are 2–3 lines at most.
- Docstrings are descriptive but not exhaustive, and written for people reading the API, not for agents.
- Long-form explanation belongs in `docs/` (the guide or a hardware doc), not in code.
- No commented-out code, no change history in comments.

## Errors

- Fail fast and explicitly with the package's `DeviceError` hierarchy.
- Error messages say what failed, on which device, and what to try next when there is a known remedy (e.g. `DeviceBusy` naming `ptpcamerad`).
- Never swallow exceptions silently. If an error is genuinely ignorable, log it at debug level and say why in a comment.

## Hardware facts

- A constant that encodes hardware behavior (a timeout, a settle delay, a baud rate, a magic byte) is named, and either cites its source or links to the hardware doc that does.
- Don't encode one unit's or one session's measurements as properties of the hardware.
