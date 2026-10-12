# Pending Approvals: Foundation

The maintainer was away while this track ran, so phase checkpoints and approval gates
(workflow.md) were recorded here instead of blocking. Work continued on branch
`foundation_20261012`; nothing has been pushed or opened as a PR.

Later phases build on the unapproved earlier ones, so a change requested in an early item
may ripple forward. Each item names where to look.

## Do first

0. **Commit the staged changes.** Commit signing (SSH through 1Password) started failing
   near the end with `1Password: failed to fill whole buffer`, probably because 1Password
   needed an unlock or approval while you were away. I didn't bypass signing. The
   remaining changes are staged in the working tree:
   `git commit -m "Finish foundation track bookkeeping"`. They include untracking
   `.coverage`, which my first commit picked up by accident (squash-merge drops it), and
   ruff formatting of the guide's code blocks, without which CI's format check would
   fail.
   - The earlier commits on the branch show `%G?` = `N`. They were created with the same
     signing config, so check they're signed the way you expect.

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

4. **Phase 4, contracts and simulated devices.** `tests/contracts/` plus the simulated
   devices in each role subpackage (`*/simulated.py`). Review:
   - How the contracts are wired. Driver tests subclass e.g. `LightContract` and override
     the `make_device` fixture. `pythonpath = ["tests"]` (pytest) and
     `mypy_path = ["src", "tests"]` make `contracts` importable. `tests/contracts/` is the
     one directory under `tests/` with an `__init__.py`.
   - The stage contract's stop test grabs `device._lock` from another thread to stand in
     for a running move, so it reaches into a private attribute.
   - Simulated device behaviour worth knowing:
     - `SimulatedStage`: moves take distance/speed seconds and update position as they
       go. Moving while jogging raises `DeviceBusy`. Homing drives every axis to its
       minimum (or 0).
     - `SimulatedCamera` and `SimulatedFileCamera`: captures sleep for `exposure_time`.
       ISO is mapped onto gain for brightness (ISO 100 = 0 dB).
     - `SimulatedFocusDrive`: the hidden lens position survives reopen, but the counter
       doesn't.
   - The timing-based stage tests passed 20 of 20 runs locally. They use lower bounds or
     wide margins, but slow CI runners are the risk to watch.
   - mypy now targets Python 3.11 (`python_version = "3.11"`) after I twice wrote PEP 695
     generics that 3.11 can't parse.

## Approval gates

3. **Role ABC signatures (Phase 3 gate; workflow.md requires approval).** Phase 4 and 5
   were built on these unapproved, so changes here will touch the simulated devices and
   contract suite too. Files: `cameras/base.py`, `cameras/focus.py`, `lights/base.py`,
   `stages/base.py`, `hubs/base.py`.

   Common pattern: the public method is `@synchronized`, validates against what the
   driver declares, then calls a protected hook.

   **Camera** (`Camera`, `FrameCamera`, `FileCamera`)
   - `settings() -> dict[str, Setting]`, `setting(name) -> Setting`,
     `set_setting(name, value)`, `preview_frame() -> NDArray[uint8]`.
     Hooks: `_settings()`, `_set_setting()`, `_preview_frame()`, optional `_setting()`.
   - `FrameCamera.capture_frame() -> Frame`; `FileCamera.capture_files() -> list[CameraFile]`.
   - `Setting(name, value, minimum, maximum, choices, unit, read_only)` with `validate()`.
     The type comes from `value`; an int is accepted for a float setting; a wrong type
     raises `TypeError`.
   - Canonical names are module constants: `EXPOSURE_TIME` (s), `GAIN` (dB), `ISO`,
     `APERTURE` (f-number), `PIXEL_FORMAT`, with units in `CANONICAL_UNITS`. **Question:**
     are these the right canonical set? The spec only named `exposure_time`.
   - `preview_frame()` is typed `uint8` (display-ready). **Question:** OK, or should it
     allow any dtype?
   - `Frame(pixels, pixel_format, timestamp: datetime, metadata)`.
   - `CameraFile(name, data)`, `.suffix`. `save(dest_dir, stem, overwrite=False) -> Path`
     keeps the camera's extension, opens with `"xb"` (so the refusal is atomic), rejects
     stems with path separators, and does **not** create `dest_dir`.

   **FocusDrive**
   - `step(steps: int)` (positive = toward far), `drive_to_limit("near" | "far")`,
     `focus_steps -> int | None`, `last_limit -> "near" | "far" | None`.
     Hooks: `_step()`, `_drive_to_limit()`.
   - A `_step()` that raises `FocusLimitReached(limit)` resets the counter to 0 at that
     limit. The counter is forgotten on close.
   - **Question:** I picked positive = far. Is that the Nikon/gphoto2 convention you want?

   **Light**
   - `channels -> tuple[Channel, ...]` (abstract property), `set_level(id, level)`,
     `set_color(id, (r, g, b))`, `all_off()`. Hooks: `_set_level(channel, level)`,
     `_set_color(channel, rgb)`, optional `_all_off()` (the default zeroes each channel).
   - `Channel(id, kind=ChannelKind.MONO | RGB, wavelength_nm=None)`.
   - **Decision:** `set_level()` on an RGB channel sets r = g = b = level rather than
     raising. Hooks receive the `Channel` object, not just the ID.
   - There is no getter for current levels (not in spec; lab firmware may not report it).

   **Stage**
   - `axes`, `move_to(targets: Mapping[str, float], timeout=None)`,
     `move_by(offsets, timeout=None)`, `jog(axis, direction: ±1)`, `stop()`,
     `position() -> dict[str, float]`, `home(timeout=None)`, `is_homed`,
     class attributes `move_timeout = 60.0` and `requires_homing = False`.
   - Moves take a mapping so multi-axis stages (X-Carve) can move axes together.
   - `stop()` and `position()` **don't take the device lock** (so they work mid-move);
     their hooks must be thread-safe. An interrupted move raises `PositionError`.
   - `Axis(name, kind, minimum, maximum, wraps)`, `.unit`, `.check()`. Wrapping axes have
     no limits, report [0, 360), and `move_to` takes the shorter way round (as the Foldio
     client does). `move_by` defaults to an absolute move; drivers with native relative
     moves (Foldio: multi-turn moves taken literally) override `_move_by()`.
   - `home()` on a stage with `requires_homing = False` raises `NotImplementedError`.
     **Question:** is that the right error? Also, there's no way to say "can home but
     doesn't have to" (e.g. GRBL with homing optional).
   - Speed isn't part of the ABC (the Foldio has speeds 1, 2, 3; GRBL has a feed rate in mm/min).
     **Question:** leave speed as driver-specific constructor or attribute config?
   - `jog()` doesn't check limits; the hardware's soft limits apply.

   **Hub**
   - `ports -> tuple[int, ...]`, `power_on(port)`, `power_off(port)`,
     `is_powered(port)`, `power_cycle(port, off_time=None)`, class attribute
     `power_cycle_off_time = 1.0` (a convention, not measured). Hooks `_set_power()`,
     `_is_powered()`.

5. **Phase 5, CLI and docs.** `src/educelab/devices/app/`, `docs/guide.md`,
   `docs/hardware/README.md`, `README.md`. The CLI works in fresh 3.11, 3.12, and 3.13
   venvs with no extras.
   - Subcommands are modules in `app/` listed in `cli.SUBCOMMANDS`, each with
     `register(subparsers)`. Backends for `discover` are listed in
     `app/discover.BACKENDS` as `(name, module, class name)` strings. It's empty until
     the first driver lands, so `el-devices discover` prints "No devices found."
   - `discover` reports a backend that is missing its SDK, or that raises a
     `DeviceError`, as one line and moves on. The exit status is 0.
   - `main()` turns an uncaught `DeviceError` into a one-line message with exit code 1,
     and Ctrl-C into 130.
   - The README extras table lists every planned extra, but none are declared in
     `pyproject.toml` yet; each driver track adds its own. **Question:** declare the
     extras up front instead?
   - `docs/hardware/README.md` has an index of planned models, **verified / assumed /
     unknown** markers (matching the Foldio `PROTOCOL.md`), the generalising rule, and a
     standard set of sections. Please check the model list and names.
6. **Track completion.** All 22 tasks are done, but I left the track `[~]` in
   `tracks.md` and `in_progress` in `metadata.json` until the gates above are approved
   and CI is green. Once they are, conductor's completion steps remain: mark the track
   complete, offer a doc sync of `product.md` and `tech-stack.md` (I didn't touch them;
   nothing in them is contradicted), and archive or keep the track.
7. **Push and PR.** Not done. Per workflow.md, it's one PR for the track, opened when
   you ask.

## Decisions and deviations

- **Tests were written alongside each ABC** (Tasks 3.1–3.4) instead of all in Task 3.5.
  Task 3.5 added a test that one device can offer several roles (Stage + Light). Noted
  in plan.md.
- **`DeviceInfo` landed in Task 2.2**, not 2.4, because the lifecycle tests needed it.
- **Role state lives in class-attribute defaults** (`_homed`, `_focus_steps`), not role
  `__init__`s, so multi-role devices need no init coordination. `_reset_state()`
  chains through `super()`.
- **Additions not named in the spec:** `synchronized`, `Listener`, `DeviceEvent` and
  `Disconnected`, `CANONICAL_UNITS` and the canonical setting constants,
  `Setting.validate()`, `Axis.check()`, `Axis.unit`, `Stage.move_timeout`,
  `Hub.power_cycle_off_time`, `FocusDrive.last_limit`, `ChannelKind`, the `RGB` alias,
  and `SimulatedLight.levels`.
- **Tooling additions:** pytest `pythonpath = ["tests"]`; mypy `python_version = "3.11"`
  and `mypy_path = ["src", "tests"]`; `.coverage` added to `.gitignore`; a mypy step in
  CI's lint job with `continue-on-error: true`. CI actions were already on `@v7`, so
  there was nothing deprecated to update.
- **`CLAUDE.md`** gained a mention of the advisory mypy step and the contract suite. I
  didn't change `CONTEXT.md` (that needs approval). Terms you might want to add to it:
  *contract suite*, *event/listener*, *focus limit*.
- **mypy notes:** test functions without annotations produce `annotation-unchecked`
  notes, which are noise rather than errors. Setting `check_untyped_defs = true` would
  check them instead.
