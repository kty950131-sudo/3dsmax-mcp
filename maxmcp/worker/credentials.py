"""Windows Credential Manager access for the ARTOKE worker token."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Callable


CREDENTIAL_TARGET = "ARTOKE/MotionWorkerToken"


class CredentialError(RuntimeError):
    """Credential is unavailable or invalid."""


def _read_windows_credential(target: str) -> str | None:
    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi = ctypes.WinDLL("Advapi32.dll")
    pointer = ctypes.POINTER(CREDENTIALW)()
    advapi.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CREDENTIALW)),
    ]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        return None
    try:
        credential = pointer.contents
        blob = ctypes.string_at(
            credential.CredentialBlob,
            credential.CredentialBlobSize,
        )
        return blob.decode("utf-16-le")
    finally:
        advapi.CredFree(pointer)


def load_worker_token(
    reader: Callable[[str], str | None] = _read_windows_credential,
) -> str:
    try:
        token = reader(CREDENTIAL_TARGET)
    except (OSError, ValueError):
        token = None
    if token is None or len(token) < 32:
        raise CredentialError("ARTOKE worker credential is not configured")
    return token
