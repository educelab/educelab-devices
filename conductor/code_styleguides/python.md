# Python Style Guide

PEP 8, enforced by Ruff. `pyproject.toml` is the source of truth for tool configuration; if this guide and that file disagree, the file wins.

## Formatting and linting

- `ruff format` owns layout (default line length 88, double quotes).
- `ruff check` runs the default rules plus `I` (imports), `UP` (pyupgrade), and `B` (bugbear).
- Run `ruff check . && ruff format .` before committing.

## Imports

- Standard library, third-party, then first-party (`educelab`), as sorted by Ruff.
- **Vendor SDKs are imported lazily** inside the backend module, at the point of use or in a small loader, never at a subpackage's top level. A missing SDK raises an `ImportError` that names the extra to install (or, for PySpin, the install docs).

## Types

- Annotate all public functions and methods; the package ships `py.typed`.
- Use built-in generics and `X | None` (3.11+ syntax).
- Use `typing.Protocol` only for consumer-facing callbacks; roles are ABCs.
- mypy runs at default strictness and is advisory. Don't introduce new errors.

## Classes and data

- Roles are ABCs on the shared `Device` base. Consumers compose devices rather than subclass them.
- Constructors store configuration only and never touch hardware; `open()` connects.
- Use frozen `dataclass`es for value types (`DeviceInfo`, `Frame`, `Setting`, axis descriptions).
- Units are part of the name or the docstring: seconds for time, millimetres for linear axes, degrees for rotary axes, 0.0–1.0 for light levels.

## Concurrency

- Public methods are blocking and thread-safe, serialized by a per-device lock.
- A driver may run internal threads or a private asyncio loop when its transport requires one. These never leak into the public API.

## Logging

- One module-level logger per driver: `logger = logging.getLogger(__name__)`.
- Raw protocol traffic logs at `DEBUG`. Never `print`.

## Docstrings

- Google style (`Args:`, `Returns:`, `Raises:`), rendered by mkdocstrings.
- Short and descriptive: what it does, units, what it raises, and any hardware caveat a caller must know. Point to `docs/hardware/` for the long version.

## Tests

- pytest with `--import-mode=importlib`; tests import the installed package.
- Name tests for behavior: `test_move_outside_limits_raises`.
- Drivers are tested through an injected fake transport, not by patching vendor modules.
- Hardware tests use `@pytest.mark.hardware`.
