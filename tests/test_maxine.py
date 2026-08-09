from pathlib import Path

from src.nvidia.maxine import build_bodytrack_command, check_maxine


def test_check_maxine_lists_missing_runtime(tmp_path: Path) -> None:
    report = check_maxine(tmp_path)

    assert not report.ready
    assert "nvarbodyposeestimation" in report.missing_features
    assert "nvarbodydetection" in report.missing_features


def test_check_maxine_is_ready_for_complete_layout(tmp_path: Path) -> None:
    (tmp_path / "include").mkdir()
    (tmp_path / "include" / "nvAR.h").write_text("", encoding="utf-8")
    (tmp_path / "features" / "nvarbodyposeestimation" / "bin").mkdir(parents=True)
    (tmp_path / "features" / "nvarbodydetection" / "bin").mkdir(parents=True)
    (tmp_path / "models").mkdir()
    executable = tmp_path / "artoke" / "maxine_body34.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"exe")

    report = check_maxine(tmp_path)

    assert report.ready
    assert report.missing_files == ()
    assert report.missing_features == ()


def test_bodytrack_command_keeps_paths_as_arguments(tmp_path: Path) -> None:
    command = build_bodytrack_command(
        tmp_path / "clip one.mp4",
        tmp_path / "body.json",
        tmp_path / "sdk",
    )

    assert command[-4:] == [
        "--input",
        str(tmp_path / "clip one.mp4"),
        "--output",
        str(tmp_path / "body.json"),
    ]
