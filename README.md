# EduceLab Devices

`educelab-devices` provides Python APIs for the imaging hardware used across 
EduceLab projects: cameras, lights, turntables, gantries, and similar devices.
Device APIs are organized by hardware type (e.g. `educelab.devices.cameras`, 
`educelab.devices.lights`) and are kept as simple and dependency-light as 
possible so they can be wrapped by any consumer (CLI scripts, Qt applications, 
etc.).

# Installation
Install from PyPI:

```shell
pip install educelab-devices
pip install "educelab-devices[all]"   # with every PyPI-hosted backend SDK
```

The core package needs only numpy and includes a simulated device for every
role. Each hardware backend's SDK is an optional extra, imported only when that
backend is used:

| Extra | Backend | Added by |
|-------|---------|----------|
| `serial` | MISHA, XLC lights, X-Carve (pyserial) | MISHA lights track |
| `foldio360` | Foldio360 turntable (bleak) | Foldio360 track |
| `gphoto2` | DSLRs over gphoto2 (not on Windows) | gphoto2 camera track |
| `acroname` | Acroname USB hubs (brainstem) | Acroname hub track |
| `all` | All of the above | first driver track |

Extras are declared as their driver lands. Spinnaker cameras need PySpin,
which isn't on PyPI: install the Spinnaker SDK, then the wheel bundled with
it (the versions must match exactly).

# Usage
```python
from educelab.devices.stages import SimulatedStage

with SimulatedStage() as stage:
    stage.move_to({"x": 100.0, "y": 50.0})
    print(stage.position())
```

See [docs/guide.md](docs/guide.md) for how every device behaves (lifecycle,
threading, errors, events) and [docs/hardware/](docs/hardware/README.md) for
what we know about each piece of hardware.

## Command line
`el-devices` (or `python -m educelab.devices`) checks hardware by hand:

```shell
el-devices discover              # list devices the installed backends can find
el-devices misha                 # light each band of the attached MISHA panel
el-devices simulate              # exercise every simulated device
el-devices simulate stage light  # ...or only some roles
el-devices -v <command>          # log device traffic
```

Each driver adds a subcommand for its manual hardware check.

# Development
Either pip or [uv](https://docs.astral.sh/uv/) can be used.

With pip (25.1+):
```shell
python -m venv .venv
source .venv/bin/activate
pip install -e . --group dev
pytest
ruff check . && ruff format .
```

With uv:
```shell
uv run pytest
uv run ruff check . && uv run ruff format .
```

Also available:
```shell
pytest -m hardware          # tests that need real hardware attached
pytest --cov                # coverage report
mypy                        # type check (advisory in CI)
```

## Releasing
Pushing a `vX.Y.Z` tag publishes that release to PyPI through
`.github/workflows/publish.yml`, using PyPI trusted publishing (no API token).

1. Bump `version` in `pyproject.toml` and merge it to `main` through a PR.
2. Tag the merge commit and push the tag:
   ```shell
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

The workflow fails without publishing if the tag doesn't match the version in
`pyproject.toml`. Running it by hand from the Actions tab builds the
distributions without publishing them.

Publishing needs a one-time setup: a trusted publisher for `educelab-devices`
on PyPI (owner `educelab`, repository `educelab-devices`, workflow
`publish.yml`, environment `pypi`), and a `pypi` environment in the GitHub
repository's settings.

# Project organization
- All package code goes in `src/educelab/devices`, grouped into subpackages by
  hardware type.
- `src/educelab` must not contain any files, including `__init__.py` files.
- Add runtime dependencies to `dependencies` in `pyproject.toml` and 
  development tools to `[dependency-groups]`.
- Tests go in `tests/`. Shared role contract tests are in `tests/contracts/`.
