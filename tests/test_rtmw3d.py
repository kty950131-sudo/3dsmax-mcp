from pathlib import Path

from maxmcp.rtmw3d.runtime import (
    Rtmw3dReadiness,
    build_rtmw3d_command,
    check_rtmw3d,
)


def test_check_rtmw3d_reports_missing_runtime(tmp_path: Path) -> None:
    report = check_rtmw3d(tmp_path, tmp_path / "vendor")

    assert not report.ready
    assert "python.exe" in report.missing_files
    assert "rtmw3d-l checkpoint" in report.missing_files


def test_build_command_keeps_video_and_output_as_separate_arguments(tmp_path: Path) -> None:
    runtime = Rtmw3dReadiness(
        ready=True,
        environment=tmp_path / "env",
        repository=tmp_path / "mmpose",
        checkpoint_dir=tmp_path / "models",
        missing_files=(),
    )

    command = build_rtmw3d_command(
        tmp_path / "clip one.mp4", tmp_path / "pose one.json", runtime
    )

    assert command[-4:] == [
        "--input",
        str(tmp_path / "clip one.mp4"),
        "--output",
        str(tmp_path / "pose one.json"),
    ]


def test_command_uses_project_runner_not_mmpose_visualization_demo(tmp_path: Path) -> None:
    runtime = Rtmw3dReadiness(True, tmp_path / "env", tmp_path / "repo", tmp_path / "models", ())

    command = build_rtmw3d_command(tmp_path / "a.mp4", tmp_path / "a.json", runtime)

    assert command[1].endswith("scripts\\run-rtmw3d.py")
