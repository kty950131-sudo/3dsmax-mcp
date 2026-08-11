"""Typed client for the private ARTOKE motion-worker API."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class WorkerApiError(RuntimeError):
    """A redacted ARTOKE API failure safe to write to local logs."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    source_filename: str
    object_path: str
    download_url: str
    duration_seconds: float
    edit_revision: int = 0
    tracking_url: str | None = None
    edits_url: str | None = None


@dataclass(frozen=True)
class HeartbeatResult:
    cancel_requested: bool
    lease_expires_at: str


@dataclass(frozen=True)
class UploadTarget:
    kind: str
    object_path: str
    token: str
    signed_url: str


class ArtokeApiClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        parsed = urlsplit(base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError("ARTOKE API requires HTTPS")
        if parsed.username or parsed.password:
            raise ValueError("ARTOKE API URL must not contain credentials")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._opener = opener

    def _post(self, path: str, payload: object | None = None) -> tuple[int, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self._base_url}{path}",
            method="POST",
            data=data,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener(request, timeout=30) as response:
                body = response.read()
                return response.status, None if not body else json.loads(body)
        except HTTPError as exc:
            raise WorkerApiError(
                f"ARTOKE API returned HTTP {exc.code}",
                status=exc.code,
            ) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise WorkerApiError("ARTOKE API request failed") from None

    def claim(self) -> ClaimedJob | None:
        status, payload = self._post(
            "/api/motions/worker/claim",
            {"capabilities": {"rtmw3d": True}},
        )
        if status == 204:
            return None
        try:
            job = payload["job"]
            source = payload["source"]
            edit_revision = job.get("editRevision", 0)
            tracking_url = source.get("trackingUrl")
            edits_url = source.get("editsUrl")
            if (
                not isinstance(edit_revision, int)
                or isinstance(edit_revision, bool)
                or edit_revision < 0
            ):
                raise TypeError
            if any(
                value is not None and (not isinstance(value, str) or not value)
                for value in (tracking_url, edits_url)
            ):
                raise TypeError
            if edit_revision > 0 and (tracking_url is None or edits_url is None):
                raise TypeError
            if edit_revision == 0 and (tracking_url is not None or edits_url is not None):
                raise TypeError
            return ClaimedJob(
                job_id=str(job["id"]),
                source_filename=str(job["sourceFilename"]),
                object_path=str(source["objectPath"]),
                download_url=str(source["downloadUrl"]),
                duration_seconds=float(job["sourceDurationSeconds"]),
                edit_revision=edit_revision,
                tracking_url=tracking_url,
                edits_url=edits_url,
            )
        except (KeyError, TypeError):
            raise WorkerApiError("ARTOKE claim response is invalid") from None

    def heartbeat(
        self,
        job_id: str,
        stage: str,
        progress: int,
    ) -> HeartbeatResult:
        _, payload = self._post(
            f"/api/motions/worker/jobs/{quote(job_id, safe='')}/heartbeat",
            {"stage": stage, "progress": progress},
        )
        try:
            return HeartbeatResult(
                cancel_requested=bool(payload["cancelRequested"]),
                lease_expires_at=str(payload["leaseExpiresAt"]),
            )
        except (KeyError, TypeError):
            raise WorkerApiError("ARTOKE heartbeat response is invalid") from None

    def authorize_uploads(self, job_id: str) -> tuple[UploadTarget, ...]:
        _, payload = self._post(
            f"/api/motions/worker/jobs/{quote(job_id, safe='')}/uploads"
        )
        try:
            return tuple(
                UploadTarget(
                    kind=str(item["kind"]),
                    object_path=str(item["objectPath"]),
                    token=str(item["token"]),
                    signed_url=str(item["signedUrl"]),
                )
                for item in payload["uploads"]
            )
        except (KeyError, TypeError):
            raise WorkerApiError("ARTOKE upload response is invalid") from None

    def publish(self, job_id: str, artifacts: list[dict[str, object]]) -> None:
        self._post(
            "/api/motions/worker/artifacts",
            {"jobId": job_id, "artifacts": artifacts},
        )

    def finish_failed(self, job_id: str, error_code: str) -> None:
        self._post(
            f"/api/motions/worker/jobs/{quote(job_id, safe='')}/terminal",
            {"status": "failed", "errorCode": error_code},
        )

    def finish_cancelled(self, job_id: str) -> None:
        self._post(
            f"/api/motions/worker/jobs/{quote(job_id, safe='')}/terminal",
            {"status": "cancelled"},
        )
