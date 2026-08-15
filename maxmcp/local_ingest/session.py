"""Single-browser state and bounded source reception for the local companion."""

from __future__ import annotations

from dataclasses import dataclass
from io import BufferedIOBase
import os
from pathlib import Path
import re
import secrets
import threading
from typing import BinaryIO
from uuid import uuid4

from maxmcp.worker.workspace import JobWorkspace


MAX_SOURCE_BYTES = 2_147_483_648
DEFAULT_CHUNK_SIZE = 1024 * 1024
_DISPLAY_NAME_LIMIT = 180
_UNSAFE_DISPLAY_CHARACTERS = re.compile(r"[\\/\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")


class SessionRejected(RuntimeError):
    """A stable, safe browser-session rejection."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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


@dataclass(frozen=True)
class SessionSnapshot:
    state: str
    size_bytes: int | None


class CompanionSession:
    """Own one fresh workspace, one browser, and at most one source upload."""

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
        self.browser_cookie = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.display_name: str | None = None
        self.source_path: Path | None = None
        self._source_size: int | None = None
        self._browser_claimed = False
        self._upload_claimed = False
        self._upload_active = False
        self._cancelled = threading.Event()
        self._closed = False
        self._cleaned = False
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

    def claim_browser(self, cookie: str | None) -> bool:
        """Claim the sole browser; return whether a cookie must be issued."""
        with self._lock:
            if self._closed:
                raise SessionRejected("browser_session_closed")
            if cookie == self.browser_cookie and self._browser_claimed:
                return False
            if cookie is None and not self._browser_claimed:
                self._browser_claimed = True
                return True
        raise SessionRejected("browser_session_in_use")

    def authorize_browser(self, cookie: str | None) -> None:
        with self._lock:
            valid = (
                not self._closed
                and self._browser_claimed
                and cookie is not None
                and secrets.compare_digest(cookie, self.browser_cookie)
            )
        if not valid:
            raise SessionRejected("browser_session_required")

    def authorize_mutation(self, cookie: str | None, csrf: str | None) -> None:
        self.authorize_browser(cookie)
        if csrf is None or not secrets.compare_digest(csrf, self.csrf_token):
            raise SessionRejected("csrf_rejected")

    def snapshot(self) -> SessionSnapshot:
        with self._lock:
            if self._cancelled.is_set():
                state = "cancelled"
            elif self.source_path is not None:
                state = "source_received"
            elif self._upload_active:
                state = "receiving"
            else:
                state = "waiting_for_source"
            return SessionSnapshot(state=state, size_bytes=self._source_size)

    def receive_source(
        self,
        stream: BinaryIO | BufferedIOBase,
        content_length: int,
        display_name: str,
    ) -> Path:
        if not isinstance(content_length, int) or isinstance(content_length, bool):
            raise UploadRejected("invalid_content_length")
        if content_length <= 0:
            raise UploadRejected("empty_source")
        if content_length > self.max_source_bytes:
            raise UploadRejected("source_too_large")

        with self._lock:
            if self._closed or self._cancelled.is_set():
                raise UploadRejected("cancelled")
            if self._upload_claimed:
                raise UploadRejected("source_already_selected")
            self._upload_claimed = True
            self._upload_active = True

        partial: Path | None = None
        final: Path | None = None
        try:
            partial, final = self._new_source_targets()
            remaining = content_length
            with partial.open("xb") as destination:
                while remaining:
                    if self._cancelled.is_set():
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
            if self._cancelled.is_set():
                raise UploadRejected("cancelled")
            os.replace(partial, final)
            with self._lock:
                self.display_name = _display_name(display_name)
                self.source_path = final
                self._source_size = content_length
            return final
        except UploadRejected:
            if partial is not None:
                self._safe_unlink(partial)
            if final is not None:
                self._safe_unlink(final)
            raise
        except (OSError, ValueError):
            if partial is not None:
                self._safe_unlink(partial)
            if final is not None:
                self._safe_unlink(final)
            raise UploadRejected("source_write_failed") from None
        finally:
            cleanup = False
            with self._lock:
                self._upload_active = False
                cleanup = self._cancelled.is_set() and not self._cleaned
            if cleanup:
                self._cleanup_workspace()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            cleanup = not self._upload_active and not self._cleaned
        if cleanup:
            self._cleanup_workspace()

    def close(self) -> None:
        self._cancelled.set()
        with self._lock:
            self._closed = True
            cleanup = not self._upload_active and not self._cleaned
        if cleanup:
            self._cleanup_workspace()

    def _new_source_targets(self) -> tuple[Path, Path]:
        workspace = self.workspace.path
        if (
            not workspace.is_dir()
            or _is_link(workspace)
            or workspace.resolve() != workspace
            or workspace.resolve().parent != self.workspace.path.parent.resolve()
        ):
            raise UploadRejected("unsafe_workspace")
        basename = str(uuid4())
        partial = workspace / f"{basename}.part"
        final = workspace / basename
        if partial.parent.resolve() != workspace or final.parent.resolve() != workspace:
            raise UploadRejected("unsafe_workspace")
        return partial, final

    def _cleanup_workspace(self) -> None:
        try:
            self.workspace.cleanup()
        except (OSError, RuntimeError):
            return
        with self._lock:
            self._cleaned = True

    def _safe_unlink(self, path: Path) -> None:
        workspace = self.workspace.path
        try:
            if (
                path.parent != workspace
                or not workspace.is_dir()
                or _is_link(workspace)
                or workspace.resolve() != workspace
                or path.is_symlink()
                or (path.exists() and path.resolve().parent != workspace)
            ):
                return
            path.unlink(missing_ok=True)
        except OSError:
            return
