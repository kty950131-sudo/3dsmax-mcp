from pathlib import Path

import pytest

from maxmcp.rtmw3d.runtime import Rtmw3dReadiness
from maxmcp.worker.__main__ import main


def readiness(root: Path, ready: bool = True) -> Rtmw3dReadiness:
    return Rtmw3dReadiness(
        ready, root / "env", root / "repo", root / "models",
        () if ready else ("checkpoint",),
    )


def test_cli_never_accepts_token_argument(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["run", "--token", "secret"], readiness=lambda: readiness(tmp_path))


def test_doctor_checks_runtime_and_ffmpeg_without_loading_token(
    tmp_path: Path,
    capsys,
) -> None:
    loaded = False

    def token_loader():
        nonlocal loaded
        loaded = True
        return "a" * 40

    result = main(
        ["doctor"],
        readiness=lambda: readiness(tmp_path, False),
        token_loader=token_loader,
        executable_finder=lambda _name: None,
    )

    assert result == 1
    assert loaded is False
    output = capsys.readouterr().out
    assert "RTMW3D: not ready" in output
    assert "ffmpeg: not found" in output


def test_cleanup_removes_only_stale_worker_jobs(tmp_path: Path, capsys) -> None:
    result = main(["cleanup", "--cache-root", str(tmp_path)])
    assert result == 0
    assert "Removed 0 stale job(s)." in capsys.readouterr().out
