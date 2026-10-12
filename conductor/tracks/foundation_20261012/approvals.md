# Pending Approvals: Foundation

The maintainer was away while this track ran, so phase checkpoints and approval gates
(workflow.md) were recorded here instead of blocking. Work continued on branch
`foundation_20261012`; nothing has been pushed or opened as a PR.

Later phases build on the unapproved earlier ones, so a change requested in an early item
may ripple forward. Each item names where to look.

## Phase checkpoints

1. **Phase 1, tooling.** `pytest`, `ruff check .`, `ruff format --check .`, and `mypy` pass
   locally on Python 3.14. CI has not run because the branch isn't pushed. Please push (or
   approve pushing) to confirm the 3.11–3.14 × 3 OS matrix.
   - `pyproject.toml`: numpy `>=1.26` (first release with 3.12 wheels; 3.11 also works).
   - Hardware tests are deselected with `-m "not hardware"` in `addopts`. A later `-m` on
     the command line replaces it, so `pytest -m hardware` runs them.
   - Coverage is opt-in: `pytest --cov` (configured with `source` and `branch = true`).
2. **Phase 2, `Device` base.** Review `src/educelab/devices/device.py` and `errors.py`.
   Things to look at in particular:
   - `synchronized` decorator: takes the lock and raises `NotConnected` when closed. Role
     ABC public methods use it, and drivers can use it for their own public methods.
   - `close()` always leaves the device closed, even if `_close()` raises, then calls the
     cooperative `_reset_state()` hook (roles use it to forget homing and focus position).
   - Events: `DeviceEvent(device)` base and `Disconnected(reason)`. Listeners are a
     `Listener` Protocol and are called from whatever thread emits; their exceptions are
     logged at ERROR. The listener list has its own lock, so events aren't blocked by a
     running call.
   - `add_listener` ignores duplicates; `remove_listener` of an unknown listener is a no-op.
   - `DeviceInfo` fields: `backend`, `model`, `address`, `serial_number`,
     `firmware_version`.
   - `discover()` defaults to `[]` rather than being abstract, so roles and simulated
     devices don't each need to override it.
   - `DeviceTimeout` also subclasses `TimeoutError`.
   - `FocusLimitReached` carries `.limit` (`"near"`/`"far"`).
   - `import_optional(name, extra=..., hint=...)` is the missing-extra helper. It chains
     the original `ImportError` (brainstem fails at import on unknown distros).
   - Argument errors are built-ins, not `DeviceError`s: unknown channel/axis/port/setting
     → `KeyError`, bad value → `ValueError`. Stage targets out of limits are the one
     exception and raise `PositionError`, as the spec requires.

## Approval gates

## Decisions and deviations
