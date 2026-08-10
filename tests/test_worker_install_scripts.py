from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_register_script_uses_current_user_hidden_startup() -> None:
    script = (ROOT / "scripts" / "register_artoke_worker.ps1").read_text(encoding="utf-8")
    assert "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" in script
    assert "-WindowStyle Hidden" in script
    assert "Resolve-Path" in script
    assert "pythonw.exe" in script
    assert "HKLM:" not in script


def test_unregister_script_removes_only_artoke_worker_entry() -> None:
    script = (ROOT / "scripts" / "unregister_artoke_worker.ps1").read_text(encoding="utf-8")
    assert "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" in script
    assert "Remove-ItemProperty" in script
    assert "ARTOKE Motion Worker" in script
    assert "Remove-Item -Recurse" not in script


def test_hidden_launcher_uses_absolute_repo_and_python_paths() -> None:
    script = (ROOT / "scripts" / "start_artoke_worker.ps1").read_text(encoding="utf-8")
    assert "Resolve-Path" in script
    assert "Push-Location" in script
    assert "-m src.worker run" in script
