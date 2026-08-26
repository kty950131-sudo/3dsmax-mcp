"""Per-job local workspace with bounded cleanup."""

from __future__ import annotations

from dataclasses import dataclass
import errno
from pathlib import Path
import os
import shutil
import stat
import threading
import time
from uuid import UUID, uuid4


_LIVE_WORKSPACES: set[Path] = set()
_LIVE_LOCK = threading.Lock()
_LEASE_NAME = ".lease"
_PROCESS_LOCK_NAME = ".local-ingest.lock"


def _validated_job_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("invalid job id") from None
    if str(parsed) != value.lower():
        raise ValueError("invalid job id")
    return str(parsed)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


class WorkspaceProcessLock:
    """A cross-process advisory lock scoped to one cache root."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / _PROCESS_LOCK_NAME
        self._descriptor: int | None = None

    def acquire(self) -> bool:
        if self._descriptor is not None:
            return True
        flags = os.O_RDWR | os.O_CREAT
        for optional in ("O_BINARY", "O_NOINHERIT", "O_NOFOLLOW"):
            flags |= int(getattr(os, optional, 0))
        descriptor: int | None = None
        try:
            descriptor = os.open(self.path, flags, 0o600)
            path_info = self.path.stat(follow_symlinks=False)
            handle_info = os.fstat(descriptor)
            attributes = getattr(path_info, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if (
                not stat.S_ISREG(path_info.st_mode)
                or (path_info.st_dev, path_info.st_ino)
                != (handle_info.st_dev, handle_info.st_ino)
                or attributes & reparse
                or self.path.resolve() != self.path
            ):
                raise RuntimeError("unsafe process lock")
            if os.name == "nt":
                import msvcrt

                if handle_info.st_size == 0:
                    os.write(descriptor, b"0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                return False
            raise RuntimeError("process lock unavailable") from None
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        self._descriptor = descriptor
        return True

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class JobWorkspace:
    root: Path
    job_id: str
    path: Path

    @classmethod
    def open(cls, root: Path, job_id: str) -> "JobWorkspace":
        safe_id = _validated_job_id(job_id)
        resolved_root = root.resolve()
        resolved_root.mkdir(parents=True, exist_ok=True)
        job_root = (resolved_root / safe_id).resolve()
        if job_root.parent != resolved_root or _is_link(job_root):
            raise ValueError("invalid job id")
        job_root.mkdir(exist_ok=True)
        path = (job_root / str(uuid4())).resolve()
        if path.parent != job_root:
            raise ValueError("invalid job id")
        path.mkdir(exist_ok=False)
        return cls(resolved_root, safe_id, path)

    def __enter__(self) -> "JobWorkspace":
        return self

    def __exit__(self, *_args: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        self.release()
        resolved = self.path.resolve()
        if resolved.parent.parent != self.root or _is_link(self.path):
            raise RuntimeError("workspace path escaped cache root")
        if self.path.exists():
            shutil.rmtree(self.path)
        if self.path.parent.exists() and not any(self.path.parent.iterdir()):
            self.path.parent.rmdir()

    def acquire(self) -> None:
        """Mark this unique attempt live for in-process and crash cleanup guards."""
        resolved = self.path.resolve()
        if resolved != self.path or resolved.parent.parent != self.root or _is_link(self.path):
            raise RuntimeError("workspace path escaped cache root")
        lease = self.path / _LEASE_NAME
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        for optional in ("O_BINARY", "O_NOINHERIT", "O_NOFOLLOW"):
            flags |= int(getattr(os, optional, 0))
        try:
            descriptor = os.open(lease, flags, 0o600)
        except FileExistsError:
            raise RuntimeError("workspace is already active") from None
        try:
            os.write(descriptor, b"active\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        with _LIVE_LOCK:
            _LIVE_WORKSPACES.add(resolved)

    def heartbeat(self) -> None:
        lease = self.path / _LEASE_NAME
        resolved = self.path.resolve()
        with _LIVE_LOCK:
            live = resolved in _LIVE_WORKSPACES
        if not live or _is_link(lease) or not lease.is_file():
            raise RuntimeError("workspace is not active")
        lease.touch()

    def release(self) -> None:
        with _LIVE_LOCK:
            _LIVE_WORKSPACES.discard(self.path)
        lease = self.path / _LEASE_NAME
        try:
            safe_workspace = (
                self.path.resolve() == self.path
                and self.path.parent.parent == self.root
                and not _is_link(self.path)
            )
            if safe_workspace and lease.parent == self.path and not _is_link(lease):
                lease.unlink(missing_ok=True)
        except OSError:
            return


def _tree_state(path: Path, cutoff: float) -> tuple[bool, bool, float]:
    """Return unsafe/active/newest-mtime without following links."""
    pending = [path]
    newest = path.stat(follow_symlinks=False).st_mtime
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                        attributes = getattr(info, "st_file_attributes", 0)
                        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                        if entry.is_symlink() or attributes & reparse:
                            return True, False, newest
                        newest = max(newest, info.st_mtime)
                        entry_path = Path(entry.path)
                        if entry.name == _LEASE_NAME:
                            with _LIVE_LOCK:
                                in_process = entry_path.parent in _LIVE_WORKSPACES
                            if in_process or info.st_mtime >= cutoff:
                                return False, True, newest
                        elif stat.S_ISDIR(info.st_mode):
                            pending.append(entry_path)
                    except OSError:
                        return True, False, newest
        except OSError:
            return True, False, newest
    return False, False, newest


def cleanup_stale(
    root: Path,
    older_than_seconds: int = 86_400,
) -> list[Path]:
    if _is_link(root):
        return []
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        return []
    process_lock = WorkspaceProcessLock(resolved_root)
    try:
        acquired = process_lock.acquire()
    except RuntimeError:
        return []
    if not acquired:
        return []
    cutoff = time.time() - max(older_than_seconds, 1)
    removed: list[Path] = []
    try:
        for child in resolved_root.iterdir():
            if not child.is_dir() or _is_link(child):
                continue
            try:
                _validated_job_id(child.name)
                initial = child.stat(follow_symlinks=False)
            except (ValueError, OSError):
                continue
            resolved = child.resolve()
            if resolved.parent != resolved_root or initial.st_mtime >= cutoff:
                continue
            unsafe, active, newest = _tree_state(child, cutoff)
            if unsafe or active or newest >= cutoff:
                continue
            try:
                current = child.stat(follow_symlinks=False)
                unsafe_now, active_now, newest_now = _tree_state(child, cutoff)
            except OSError:
                continue
            if (
                unsafe_now
                or active_now
                or newest_now >= cutoff
                or (current.st_dev, current.st_ino) != (initial.st_dev, initial.st_ino)
                or not stat.S_ISDIR(current.st_mode)
                or child.resolve() != resolved
            ):
                continue
            shutil.rmtree(child)
            removed.append(resolved)
        return removed
    finally:
        process_lock.release()
