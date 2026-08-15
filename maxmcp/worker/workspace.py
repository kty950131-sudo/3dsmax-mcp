"""Per-job local workspace with bounded cleanup."""

from __future__ import annotations

from dataclasses import dataclass
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


def _tree_state(path: Path, cutoff: float) -> tuple[bool, bool]:
    """Return (unsafe reparse found, active lease found) without following links."""
    pending = [path]
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
                            return True, False
                        entry_path = Path(entry.path)
                        if entry.name == _LEASE_NAME:
                            with _LIVE_LOCK:
                                in_process = entry_path.parent in _LIVE_WORKSPACES
                            if in_process or info.st_mtime >= cutoff:
                                return False, True
                        elif stat.S_ISDIR(info.st_mode):
                            pending.append(entry_path)
                    except OSError:
                        return True, False
        except OSError:
            return True, False
    return False, False


def cleanup_stale(
    root: Path,
    older_than_seconds: int = 86_400,
) -> list[Path]:
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        return []
    cutoff = time.time() - max(older_than_seconds, 1)
    removed: list[Path] = []
    for child in resolved_root.iterdir():
        if not child.is_dir() or _is_link(child):
            continue
        try:
            _validated_job_id(child.name)
        except ValueError:
            continue
        resolved = child.resolve()
        if resolved.parent != resolved_root or child.stat().st_mtime >= cutoff:
            continue
        unsafe, active = _tree_state(child, cutoff)
        if unsafe or active:
            continue
        shutil.rmtree(child)
        removed.append(resolved)
    return removed
