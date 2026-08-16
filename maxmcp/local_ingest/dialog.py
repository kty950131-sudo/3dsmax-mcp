"""Native Windows file-open dialog for choosing one local source video."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Callable, Tuple

_BUFFER_CHARS = 32_768
_TITLE = "분석할 영상 선택"
_FILTER = "동영상 파일 (*.mp4, *.mov, *.avi)\0*.mp4;*.mov;*.avi\0\0"
_OFN_EXPLORER = 0x00080000
_OFN_FILEMUSTEXIST = 0x00001000
_OFN_HIDEREADONLY = 0x00000004
_OFN_NOCHANGEDIR = 0x00000008
_OFN_NODEREFERENCELINKS = 0x00100000
_OFN_PATHMUSTEXIST = 0x00000800


class FileDialogError(RuntimeError):
    """The native file dialog could not be shown or answered safely."""


def _show(buffer: ctypes.Array) -> Tuple[int, int]:
    """Run GetOpenFileNameW into ``buffer``; return (result, extended error)."""
    if os.name != "nt":
        raise FileDialogError("file_dialog_unsupported")
    from ctypes import wintypes

    class OpenFileNameW(ctypes.Structure):
        _fields_ = (
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        )

    comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
    comdlg32.GetOpenFileNameW.argtypes = (ctypes.POINTER(OpenFileNameW),)
    comdlg32.GetOpenFileNameW.restype = wintypes.BOOL
    comdlg32.CommDlgExtendedError.argtypes = ()
    comdlg32.CommDlgExtendedError.restype = wintypes.DWORD

    options = OpenFileNameW()
    options.lStructSize = ctypes.sizeof(OpenFileNameW)
    options.lpstrFilter = _FILTER
    options.nFilterIndex = 1
    options.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
    options.nMaxFile = len(buffer)
    options.lpstrTitle = _TITLE
    options.Flags = (
        _OFN_EXPLORER
        | _OFN_FILEMUSTEXIST
        | _OFN_HIDEREADONLY
        | _OFN_NOCHANGEDIR
        | _OFN_NODEREFERENCELINKS
        | _OFN_PATHMUSTEXIST
    )
    result = comdlg32.GetOpenFileNameW(ctypes.byref(options))
    return int(result), int(comdlg32.CommDlgExtendedError())


def pick_video_file(
    show: Callable[[ctypes.Array], Tuple[int, int]] = _show,
) -> Path | None:
    """Return the chosen video path, or None when the user cancels."""
    buffer = ctypes.create_unicode_buffer(_BUFFER_CHARS)
    try:
        result, extended_error = show(buffer)
    except FileDialogError:
        raise
    except (OSError, ValueError, AttributeError, TypeError, ctypes.ArgumentError):
        raise FileDialogError("file_dialog_failed") from None
    if result:
        selected = buffer.value
        if not selected:
            raise FileDialogError("file_dialog_failed")
        return Path(selected)
    if extended_error:
        raise FileDialogError("file_dialog_failed")
    return None


__all__ = ["FileDialogError", "pick_video_file"]
