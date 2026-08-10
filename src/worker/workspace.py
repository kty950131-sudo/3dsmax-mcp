"""Per-job local workspace with bounded cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import time
from uuid import UUID


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
        path = (resolved_root / safe_id).resolve()
        if path.parent != resolved_root:
            raise ValueError("invalid job id")
        path.mkdir(exist_ok=False)
        return cls(resolved_root, safe_id, path)

    def __enter__(self) -> "JobWorkspace":
        return self

    def __exit__(self, *_args: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        resolved = self.path.resolve()
        if resolved.parent != self.root or _is_link(self.path):
            raise RuntimeError("workspace path escaped cache root")
        if self.path.exists():
            shutil.rmtree(self.path)


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
        shutil.rmtree(child)
        removed.append(resolved)
    return removed
