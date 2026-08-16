from pathlib import Path
import shutil
import subprocess
import tomllib
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "scripts" / "register_artoke_motion_protocol.ps1"
UNREGISTER = ROOT / "scripts" / "unregister_artoke_motion_protocol.ps1"
PROTOCOL_KEY = "HKCU:\\Software\\Classes\\artoke-motion"


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _run_script(script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    executable = _powershell()
    assert executable is not None
    return subprocess.run(
        [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


requires_powershell = pytest.mark.skipif(
    _powershell() is None, reason="PowerShell is unavailable"
)


def test_register_script_targets_only_the_current_user_protocol_key() -> None:
    script = REGISTER.read_text(encoding="utf-8")
    assert PROTOCOL_KEY in script
    assert "URL Protocol" in script
    assert "-m maxmcp.local_ingest ingest" in script
    assert '"%1"' in script
    assert "Mandatory = $true" in script
    assert "PythonPath" in script
    assert "IsControl" in script
    assert "HKLM" not in script
    assert "Invoke-Expression" not in script
    assert "Start-Process" not in script
    assert "Get-Command" not in script
    assert "RunAs" not in script


def test_register_script_serializes_before_any_registry_write() -> None:
    script = REGISTER.read_text(encoding="utf-8")
    assert "DryRun" in script
    assert 0 < script.index("DryRun") < script.index("New-Item")


def test_unregister_script_removes_only_the_exact_protocol_subtree() -> None:
    script = UNREGISTER.read_text(encoding="utf-8")
    assert PROTOCOL_KEY in script
    assert "Test-Path" in script
    assert "Remove-Item" in script
    assert "HKEY_CURRENT_USER\\Software\\Classes\\artoke-motion" in script
    assert "HKLM" not in script
    assert "Invoke-Expression" not in script
    assert "*" not in script


@requires_powershell
def test_register_dry_run_quotes_interpreter_paths_with_spaces(tmp_path: Path) -> None:
    interpreter = tmp_path / "py dir with spaces" / "python w.exe"
    interpreter.parent.mkdir()
    interpreter.write_text("", encoding="utf-8")
    result = _run_script(REGISTER, "-PythonPath", str(interpreter), "-DryRun")
    assert result.returncode == 0, result.stderr
    expected = f'"{interpreter}" -m maxmcp.local_ingest ingest "%1"'
    assert result.stdout.strip().lower() == expected.lower()


@requires_powershell
def test_register_dry_run_is_idempotent(tmp_path: Path) -> None:
    interpreter = tmp_path / "python.exe"
    interpreter.write_text("", encoding="utf-8")
    first = _run_script(REGISTER, "-PythonPath", str(interpreter), "-DryRun")
    second = _run_script(REGISTER, "-PythonPath", str(interpreter), "-DryRun")
    assert first.returncode == 0
    assert first.stdout == second.stdout


@requires_powershell
def test_register_refuses_a_missing_interpreter(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "python.exe"
    result = _run_script(REGISTER, "-PythonPath", str(missing), "-DryRun")
    assert result.returncode != 0


@requires_powershell
def test_register_refuses_quote_sensitive_interpreter_paths(tmp_path: Path) -> None:
    hostile = str(tmp_path / "bad'path.exe")
    result = _run_script(REGISTER, "-PythonPath", hostile, "-DryRun")
    assert result.returncode != 0


@requires_powershell
def test_unregister_dry_run_succeeds_without_mutation() -> None:
    result = _run_script(UNREGISTER, "-DryRun")
    assert result.returncode == 0


def test_console_entry_point_is_declared() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["artoke-motion"] == "maxmcp.local_ingest.__main__:main"


def test_wheel_packages_local_ingest_modules_and_entry_point(
    tmp_path: Path,
) -> None:
    wheel_builder = pytest.importorskip("hatchling.builders.wheel")
    builder = wheel_builder.WheelBuilder(str(ROOT))
    artifacts = list(builder.build(directory=str(tmp_path), versions=["standard"]))
    assert len(artifacts) == 1
    with zipfile.ZipFile(artifacts[0]) as wheel:
        names = set(wheel.namelist())
        assert "maxmcp/local_ingest/__main__.py" in names
        assert "maxmcp/local_ingest/dialog.py" in names
        assert not any("local_ingest/web/" in name for name in names)
        entry_points = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        declared = wheel.read(entry_points).decode("utf-8")
        assert "artoke-motion = maxmcp.local_ingest.__main__:main" in declared
