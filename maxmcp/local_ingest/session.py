"""Session state and bounded source reception for the local companion."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from io import BufferedIOBase
import os
from pathlib import Path
import re
import stat
import threading
from typing import BinaryIO, Iterator
from uuid import uuid4

from maxmcp.worker.workspace import JobWorkspace


MAX_SOURCE_BYTES = 2_147_483_648
DEFAULT_CHUNK_SIZE = 1024 * 1024
_DISPLAY_NAME_LIMIT = 180
_UNSAFE_DISPLAY_CHARACTERS = re.compile(r"[\\/\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")


class UploadRejected(RuntimeError):
    """A stable, safe source-reception rejection."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


def _display_name(value: str) -> str:
    if not isinstance(value, str):
        return "video"
    cleaned = _UNSAFE_DISPLAY_CHARACTERS.sub("_", value.strip())
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return (cleaned[:_DISPLAY_NAME_LIMIT].strip() or "video")


def _windows_handle_matches_path(path: Path, descriptor: int) -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
        get_final_path = kernel32.GetFinalPathNameByHandleW
        get_final_path.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        get_final_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32_768)
        length = get_final_path(handle, buffer, len(buffer), 0)
        if length == 0 or length >= len(buffer):
            return False
        final_path = buffer.value
        if final_path.startswith("\\\\?\\UNC\\"):
            final_path = "\\\\" + final_path[8:]
        elif final_path.startswith("\\\\?\\"):
            final_path = final_path[4:]
        if os.path.normcase(os.path.abspath(final_path)) != os.path.normcase(
            os.path.abspath(path)
        ):
            return False

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = (
                ("file_attributes", wintypes.DWORD),
                ("reparse_tag", wintypes.DWORD),
            )

        get_handle_info = kernel32.GetFileInformationByHandleEx
        get_handle_info.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        get_handle_info.restype = wintypes.BOOL
        info = FileAttributeTagInfo()
        if not get_handle_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return False
        return not bool(info.file_attributes & 0x400)
    except (AttributeError, OSError, ValueError):
        return False


@dataclass(frozen=True)
class SessionSnapshot:
    state: str
    size_bytes: int | None
    cleaned: bool


@dataclass(frozen=True)
class VerifiedSourceLease:
    """A validated, handle-backed source view with no authoritative path."""

    stream: BinaryIO
    size_bytes: int
    display_name: str


class CompanionSession:
    """Own one fresh workspace and at most one accepted source."""

    def __init__(
        self,
        workspace: JobWorkspace,
        *,
        max_source_bytes: int = MAX_SOURCE_BYTES,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        if not 0 < max_source_bytes <= MAX_SOURCE_BYTES:
            raise ValueError("max_source_bytes is invalid")
        if not 0 < chunk_size <= 8 * 1024 * 1024:
            raise ValueError("chunk_size is invalid")
        self.workspace = workspace
        self.max_source_bytes = max_source_bytes
        self.chunk_size = chunk_size
        workspace_stat = workspace.path.stat(follow_symlinks=False)
        self._workspace_identity = (workspace_stat.st_dev, workspace_stat.st_ino)
        self.display_name: str | None = None
        self._source_path: Path | None = None
        self._source_descriptor: int | None = None
        self._source_size: int | None = None
        self._state = "ready"
        self._terminal_target = "cancelled"
        self._cancel_requested = threading.Event()
        self._cleaned = False
        self._cleanup_running = False
        self._lease_active = False
        self._receive_active = False
        self._lock = threading.Lock()

    @classmethod
    def create(
        cls,
        root: Path,
        job_id: str,
        *,
        max_source_bytes: int = MAX_SOURCE_BYTES,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> "CompanionSession":
        return cls(
            JobWorkspace.open(root, job_id),
            max_source_bytes=max_source_bytes,
            chunk_size=chunk_size,
        )

    def snapshot(self) -> SessionSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def receive_source(
        self,
        stream: BinaryIO | BufferedIOBase,
        content_length: int,
        display_name: str,
    ) -> SessionSnapshot:
        if not isinstance(content_length, int) or isinstance(content_length, bool):
            raise UploadRejected("invalid_content_length")
        if content_length <= 0:
            raise UploadRejected("empty_source")
        if content_length > self.max_source_bytes:
            raise UploadRejected("source_too_large")

        with self._lock:
            if self._state in {"cancelling", "cancelled", "cleanup_required", "closed"}:
                raise UploadRejected("cancelled")
            if self._state in {"receiving", "accepted"}:
                raise UploadRejected("source_already_selected")
            self._state = "receiving"
            self._receive_active = True

        source: Path | None = None
        descriptor: int | None = None
        accepted = False
        rejection: UploadRejected | None = None
        try:
            source, descriptor = self._open_source_file()
            with os.fdopen(descriptor, "wb", closefd=False) as destination:
                remaining = content_length
                while remaining:
                    if self._cancel_requested.is_set():
                        raise UploadRejected("cancelled")
                    chunk = stream.read(min(self.chunk_size, remaining))
                    if not chunk:
                        raise UploadRejected("incomplete_upload")
                    if not isinstance(chunk, bytes) or len(chunk) > remaining:
                        raise UploadRejected("invalid_source_stream")
                    destination.write(chunk)
                    remaining -= len(chunk)
                destination.flush()
                os.fsync(destination.fileno())
                self._validate_open_file(
                    source,
                    destination.fileno(),
                    expected_size=content_length,
                )
                with self._lock:
                    if self._state != "receiving" or self._cancel_requested.is_set():
                        raise UploadRejected("cancelled")
                    self.display_name = _display_name(display_name)
                    self._source_path = source
                    self._source_descriptor = descriptor
                    self._source_size = content_length
                    self._state = "accepted"
                    accepted = True
                    descriptor = None
        except UploadRejected as exc:
            rejection = exc
        except (OSError, ValueError):
            rejection = UploadRejected("source_write_failed")
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            removed = True
            if not accepted:
                removed = source is None or self._safe_unlink(source)
            cleanup_now = False
            with self._lock:
                self._receive_active = False
                if not accepted:
                    cancellation = (
                        self._state == "cancelling"
                        or self._cancel_requested.is_set()
                    )
                    if not removed:
                        self._state = "cleanup_required"
                    elif cancellation:
                        self._state = "cancelling"
                        cleanup_now = True
                    else:
                        self._state = "ready"
                elif self._state == "cancelling":
                    cleanup_now = True
            if cleanup_now:
                self._finish_cleanup()
        if accepted:
            with self._lock:
                if self._state == "accepted":
                    return self._snapshot_locked()
            raise UploadRejected("cancelled")
        if rejection is not None:
            raise rejection
        raise UploadRejected("source_write_failed")

    def cancel(self) -> SessionSnapshot:
        return self._request_terminal("cancelled")

    def close(self) -> SessionSnapshot:
        return self._request_terminal("closed")

    @contextmanager
    def open_verified_source(self) -> Iterator[VerifiedSourceLease]:
        """Yield the accepted bytes only after identity, size, and parent checks.

        The lease intentionally contains no filesystem path. Task 9 must consume
        the handle-backed stream inside this context so a stale path is never
        treated as the authoritative source.
        """
        duplicate: int | None = None
        with self._lock:
            if (
                self._state != "accepted"
                or self._source_path is None
                or self._source_descriptor is None
                or self._source_size is None
                or self.display_name is None
            ):
                raise UploadRejected("source_unavailable")
            if self._lease_active:
                raise UploadRejected("source_in_use")
            try:
                self._validate_open_file(
                    self._source_path,
                    self._source_descriptor,
                    expected_size=self._source_size,
                )
                duplicate = os.dup(self._source_descriptor)
                os.lseek(duplicate, 0, os.SEEK_SET)
                self._validate_open_file(
                    self._source_path,
                    duplicate,
                    expected_size=self._source_size,
                )
            except (OSError, UploadRejected):
                if duplicate is not None:
                    try:
                        os.close(duplicate)
                    except OSError:
                        pass
                self._state = "cleanup_required"
                self._cancel_requested.set()
                raise UploadRejected("source_identity_changed") from None
            try:
                stream = os.fdopen(duplicate, "rb", closefd=True)
            except OSError:
                try:
                    os.close(duplicate)
                except OSError:
                    pass
                self._state = "cleanup_required"
                self._cancel_requested.set()
                raise UploadRejected("source_identity_changed") from None
            self._lease_active = True
            lease = VerifiedSourceLease(
                stream=stream,
                size_bytes=self._source_size,
                display_name=self.display_name,
            )
            duplicate = None
        try:
            with lease.stream:
                yield lease
        finally:
            cleanup_now = False
            with self._lock:
                self._lease_active = False
                cleanup_now = self._state == "cancelling"
            if cleanup_now:
                self._finish_cleanup()

    def _request_terminal(self, target: str) -> SessionSnapshot:
        self._cancel_requested.set()
        with self._lock:
            self._terminal_target = target
            if self._state == target and self._cleaned:
                return self._snapshot_locked()
            if self._receive_active or self._lease_active:
                self._state = "cancelling"
                return self._snapshot_locked()
            if self._cleaned:
                self._state = target
                return self._snapshot_locked()
            self._state = "cancelling"
        return self._finish_cleanup()

    def _finish_cleanup(self) -> SessionSnapshot:
        with self._lock:
            if self._receive_active or self._lease_active:
                self._state = "cancelling"
                return self._snapshot_locked()
            if self._cleanup_running:
                return self._snapshot_locked()
            self._cleanup_running = True
            descriptor = self._source_descriptor
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                with self._lock:
                    self._cleanup_running = False
                    self._state = "cleanup_required"
                    return self._snapshot_locked()
            with self._lock:
                if self._source_descriptor == descriptor:
                    self._source_descriptor = None
        try:
            self.workspace.cleanup()
        except (OSError, RuntimeError):
            with self._lock:
                self._cleanup_running = False
                self._state = "cleanup_required"
                self._cleaned = False
                return self._snapshot_locked()
        with self._lock:
            self._cleanup_running = False
            self._state = self._terminal_target
            self._cleaned = True
            self._source_path = None
            return self._snapshot_locked()

    def _open_source_file(self) -> tuple[Path, int]:
        workspace = self.workspace.path
        if not self._workspace_is_safe():
            raise UploadRejected("unsafe_workspace")
        source = workspace / str(uuid4())
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        for optional in ("O_BINARY", "O_NOINHERIT", "O_NOFOLLOW"):
            flags |= int(getattr(os, optional, 0))
        try:
            descriptor = os.open(source, flags, 0o600)
        except OSError:
            raise UploadRejected("source_write_failed") from None
        try:
            self._validate_open_file(source, descriptor)
        except BaseException:
            os.close(descriptor)
            self._safe_unlink(source)
            raise
        return source, descriptor

    def _validate_open_file(
        self,
        path: Path,
        descriptor: int,
        *,
        expected_size: int | None = None,
    ) -> None:
        if not self._workspace_is_safe() or path.parent != self.workspace.path:
            raise UploadRejected("unsafe_workspace")
        try:
            path_stat = path.stat(follow_symlinks=False)
            handle_stat = os.fstat(descriptor)
        except OSError:
            raise UploadRejected("unsafe_workspace") from None
        if (
            _is_link(path)
            or not stat.S_ISREG(path_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino) != (handle_stat.st_dev, handle_stat.st_ino)
            or path.resolve() != path
            or not _windows_handle_matches_path(path, descriptor)
            or (expected_size is not None and handle_stat.st_size != expected_size)
        ):
            raise UploadRejected("unsafe_workspace")

    def _workspace_is_safe(self) -> bool:
        workspace = self.workspace.path
        try:
            workspace_stat = workspace.stat(follow_symlinks=False)
            return (
                workspace.is_dir()
                and not _is_link(workspace)
                and (workspace_stat.st_dev, workspace_stat.st_ino)
                == self._workspace_identity
                and workspace.resolve() == workspace
                and workspace.resolve().parent == self.workspace.path.parent.resolve()
            )
        except OSError:
            return False

    def _safe_unlink(self, path: Path) -> bool:
        workspace = self.workspace.path
        try:
            if (
                path.parent != workspace
                or not self._workspace_is_safe()
                or _is_link(path)
                or (path.exists() and path.resolve().parent != workspace)
            ):
                return False
            path.unlink(missing_ok=True)
            return not path.exists()
        except OSError:
            return False

    def _snapshot_locked(self) -> SessionSnapshot:
        return SessionSnapshot(
            state=self._state,
            size_bytes=self._source_size,
            cleaned=self._cleaned,
        )
