import ctypes
from pathlib import Path

import pytest

from maxmcp.local_ingest.dialog import (
    FileDialogError,
    _show,
    pick_video_file,
)


class FakeFunction:
    def __init__(self, implementation):
        self.implementation = implementation

    def __call__(self, *args):
        return self.implementation(*args)


def test_selected_path_is_returned(tmp_path: Path) -> None:
    selected = tmp_path / "clip.mp4"

    def show(buffer: object) -> tuple[int, int]:
        buffer.value = str(selected)  # type: ignore[attr-defined]
        return 1, 0

    assert pick_video_file(show=show) == selected


def test_user_cancel_returns_none() -> None:
    assert pick_video_file(show=lambda _buffer: (0, 0)) is None


def test_native_dialog_is_owned_by_the_foreground_window() -> None:
    captured: dict[str, object] = {}

    def get_open_file_name(pointer: object) -> int:
        captured["owner"] = pointer._obj.hwndOwner  # type: ignore[attr-defined]
        return 0

    class User32:
        GetForegroundWindow = FakeFunction(lambda: 0x1234)

    class Comdlg32:
        GetOpenFileNameW = FakeFunction(get_open_file_name)
        CommDlgExtendedError = FakeFunction(lambda: 0)

    def load_library(name: str, *, use_last_error: bool):
        assert use_last_error is True
        return User32() if name == "user32" else Comdlg32()

    result, error = _show(
        ctypes.create_unicode_buffer(1024),
        load_library=load_library,
    )

    assert (result, error) == (0, 0)
    assert captured["owner"] == 0x1234


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
