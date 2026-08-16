from pathlib import Path

import pytest

from maxmcp.local_ingest.dialog import (
    FileDialogError,
    pick_video_file,
)


def test_selected_path_is_returned(tmp_path: Path) -> None:
    selected = tmp_path / "clip.mp4"

    def show(buffer: object) -> tuple[int, int]:
        buffer.value = str(selected)  # type: ignore[attr-defined]
        return 1, 0

    assert pick_video_file(show=show) == selected


def test_user_cancel_returns_none() -> None:
    assert pick_video_file(show=lambda _buffer: (0, 0)) is None


def test_dialog_failure_raises() -> None:
    with pytest.raises(FileDialogError):
        pick_video_file(show=lambda _buffer: (0, 0x7777))


def test_success_with_empty_buffer_raises() -> None:
    with pytest.raises(FileDialogError):
        pick_video_file(show=lambda _buffer: (1, 0))


def test_show_exception_is_wrapped() -> None:
    def show(_buffer: object) -> tuple[int, int]:
        raise OSError("comdlg32 unavailable")

    with pytest.raises(FileDialogError):
        pick_video_file(show=show)
