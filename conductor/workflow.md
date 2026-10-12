# Development Workflow

## Core principles

1. **plan.md is the source of truth.** Task status and progress live in the active track's plan, not in chat.
2. **Moderate TDD, with required driver tests** (below).
3. **CI must be green** before a track is merged: Ruff, and pytest on every Python/OS in the matrix.
4. **Hardware knowledge is a deliverable.** What a track learns about a device goes into `docs/hardware/` in the same track.

## TDD: moderate, with required driver tests

Tests are encouraged everywhere and may follow exploratory code. They are **required** for:

- Every driver's protocol logic, tested against a fake transport that replays the device's protocol.
- The shared contract tests for each role, which must pass for both the real driver (on its fake transport) and the role's simulated device.
- Discovery and matching logic (e.g. serial port selection).

Tests that need real hardware are marked `@pytest.mark.hardware`. They are written alongside the driver but never block CI.

## Task lifecycle

1. **Select** the next pending task in `plan.md`; check its dependencies.
2. **Mark** it `[~]`.
3. **Red:** write the test that pins the behavior and confirm it fails for the right reason.
4. **Green:** write the minimum code to pass.
5. **Refactor:** apply `code_styleguides/`.
6. **Deviations:** if the implementation differs from the spec, note why in `plan.md`. If it changes what we know about the hardware, update `docs/hardware/`.
7. **Commit** related changes with a descriptive, imperative subject line (no Conventional Commits prefix).
8. **Mark** the task `[x]` in `plan.md`, in the same commit or a follow-up.

## Branches and pull requests

- Track work happens on a branch named for the track and lands on `main` as **at most one PR per track**, squash-merged.
- **Never open a PR per task or phase**, and don't open a PR without the maintainer asking.
- Small doc, glossary, or config fixes outside a track commit directly to `main`.

## Phase completion

1. All phase tasks are `[x]`.
2. Full suite passes: `pytest`, `ruff check .`, `ruff format --check .`.
3. Stop for maintainer verification before starting the next phase.

### Approval required

Get maintainer approval before:

- Changing a role ABC's signature (Camera, Light, Stage, Hub, Focus drive) or the `Device` base.
- Adding or changing terms in `CONTEXT.md`.
- Adding or superseding an ADR.

### Driver tracks

The final phase of a driver track includes a manual check on real hardware using the track's `el-devices` subcommand, and the `hardware`-marked tests run against the device. Record results (firmware version, body, anything surprising) in the device's hardware doc.

## Quality gates

| Gate | Command | Enforced |
|------|---------|----------|
| Tests | `pytest` | CI, blocking |
| Lint | `ruff check .` | CI, blocking |
| Format | `ruff format --check .` | CI, blocking |
| Coverage | `pytest --cov --cov-report=term` | Reported, no threshold |
| Types | `mypy` | CI, advisory |
| Hardware docs | `docs/hardware/<model>.md` updated | Review |
| Hardware check | `el-devices <subcommand>` + `pytest -m hardware` | Manual, driver tracks |
