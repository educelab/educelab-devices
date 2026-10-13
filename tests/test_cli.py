import subprocess
import sys
from importlib.metadata import entry_points

import pytest

import educelab.devices
from educelab.devices.app.cli import main


def test_help_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--help"])
    assert info.value.code == 0
    assert "el-devices" in capsys.readouterr().out


def test_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert educelab.devices.__version__ in capsys.readouterr().out


def test_missing_subcommand_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == 2


def test_console_script_is_registered():
    scripts = entry_points(group="console_scripts")
    (script,) = [ep for ep in scripts if ep.name == "el-devices"]
    assert script.value == "educelab.devices.app.cli:main"


def test_module_runs_the_cli():
    result = subprocess.run(
        [sys.executable, "-m", "educelab.devices", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "el-devices" in result.stdout


def test_cli_starts_without_importing_devices():
    code = (
        "import sys; from educelab.devices.app.cli import build_parser; "
        "build_parser(); "
        "print(sorted(m for m in sys.modules if m == 'numpy' or '.simulated' in m))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"
