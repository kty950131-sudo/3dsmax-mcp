"""Job-scoped client for the ARTOKE local motion ingest API."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import math
import re
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID


_MAX_RESPONSE_BYTES = 1024 * 1024
_REQUEST_TIMEOUT_SECONDS = 15.0
_JOB_STATUSES = {
    "uploading",
    "queued",
    "waiting_for_worker",
    "processing",
    "generating_bvh",
    "completed",
    "failed",
    "cancel_requested",
    "cancelled",
    "deleting",
}
_PROGRESS_STAGES = {"downloading", "extracting", "converting", "validating", "uploading"}
_JOB_PROGRESS_STAGES = _PROGRESS_STAGES | {"queued", "completed", "failed", "cancelled"}
_ARTIFACT_KINDS = {"bvh", "rtmw3d_json", "thumbnail", "metadata"}
_SOURCE_KEYS = {
    "name",
    "filename",
    "contentType",
    "sizeBytes",
    "durationSeconds",
    "sourceSha256",
}
_ARTIFACT_KEYS = {"kind", "sizeBytes", "sha256", "formatVersion"}
_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_VIDEO_EXTENSION = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
}
_MAX_SOURCE_BYTES = 2_147_483_648
_MAX_DURATION_SECONDS = 300.0
_MAX_EDIT_REVISION = 2_147_483_647
_ARTIFACT_SIZE_LIMITS = {
    "bvh": 64 * 1024 * 1024,
    "rtmw3d_json": 45 * 1024 * 1024,
    "thumbnail": 5 * 1024 * 1024,
    "metadata": 1024 * 1024,
}
_RFC3339_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)


class LocalIngestApiError(RuntimeError):
    """A deliberately detail-free API failure that is safe to log."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LocalIngestCancelled(RuntimeError):
    """Raised before a request when cooperative cancellation was requested."""

    def __init__(self) -> None:
        super().__init__("Local motion ingest was cancelled")


@dataclass(frozen=True, repr=False)
class LocalSession:
    session_id: str
    expires_at: str

    def __repr__(self) -> str:
        return "LocalSession(<redacted>)"


@dataclass(frozen=True, repr=False)
class LocalJob:
    job_id: str
    name: str
    status: str
    source_filename: str
    source_duration_seconds: float | None
    source_size_bytes: int
    progress: int
    source_delete_after: str | None
    source_deleted_at: str | None
    progress_stage: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    completed_at: str | None

    def __repr__(self) -> str:
        return f"LocalJob(job_id={self.job_id!r}, status={self.status!r}, progress={self.progress!r})"


@dataclass(frozen=True)
class ProgressResult:
    cancel_requested: bool


@dataclass(frozen=True)
class UploadAuthorization:
    kind: str
    upload_url: str = field(repr=False)

    def __repr__(self) -> str:
        return f"UploadAuthorization(kind={self.kind!r}, upload_url='<redacted>')"


@dataclass(frozen=True, repr=False)
class SourceUploadAuthorization:
    upload_url: str = field(repr=False)

    def __repr__(self) -> str:
        return "SourceUploadAuthorization(upload_url='<redacted>')"


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _default_open(request: Request, *, timeout: float):
    return build_opener(_RejectRedirects()).open(request, timeout=timeout)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _bounded_float(value: object, minimum: float, maximum: float) -> float:
    if not _is_number(value):
        raise TypeError
    if isinstance(value, int):
        if value < minimum or value > maximum:
            raise ValueError
        return float(value)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError
    return value


def _valid_rfc3339_timestamp(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise TypeError
    if not _RFC3339_TIMESTAMP.fullmatch(value):
        raise ValueError
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return value


def _exact_object(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise TypeError
    return value


def _header(headers: object, name: str) -> str | None:
    getheader = getattr(headers, "getheader", None)
    if callable(getheader):
        value = getheader(name)
        return value if isinstance(value, str) else None
    items = getattr(headers, "items", None)
    if callable(items):
        for key, value in items():
            if str(key).lower() == name.lower() and isinstance(value, str):
                return value
    return None


def _canonical_job_id(job_id: str) -> str:
    try:
        parsed = UUID(job_id)
    except (AttributeError, TypeError, ValueError):
        raise LocalIngestApiError("Local motion job identifier is invalid") from None
    if str(parsed) != job_id.lower():
        raise LocalIngestApiError("Local motion job identifier is invalid")
    return str(parsed)


class LocalIngestApiClient:
    """In-memory, single-session client for Task 4's companion routes."""

    def __init__(
        self,
        base_url: str,
        *,
        opener: Callable[..., Any] = _default_open,
        cancelled: Callable[[], bool] = lambda: False,
        timeout: float = _REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        parsed = urlsplit(base_url)
        try:
            parsed.port
        except ValueError:
            raise ValueError("ARTOKE API URL is invalid") from None
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError("ARTOKE API requires HTTPS except on loopback")
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("ARTOKE API URL must not contain credentials")
        if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("ARTOKE API URL must not contain a path, query, or fragment")
        try:
            safe_timeout = _bounded_float(timeout, 0.000_001, 60.0)
        except (TypeError, ValueError, OverflowError):
            raise LocalIngestApiError("ARTOKE API timeout is invalid") from None

        self._base_url = base_url.rstrip("/")
        self._opener = opener
        self._cancelled = cancelled
        self._timeout = safe_timeout
        self._access_token: str | None = None
        self._job_id: str | None = None

    def __repr__(self) -> str:
        return f"LocalIngestApiClient(base_url={self._base_url!r}, authenticated={self._access_token is not None})"

    def exchange(self, handoff_token: str) -> LocalSession:
        if self._access_token is not None:
            raise LocalIngestApiError("Local motion handoff was already exchanged")
        if not isinstance(handoff_token, str) or not re.fullmatch(r"[0-9a-f]{64}", handoff_token):
            raise LocalIngestApiError("Local motion handoff is invalid")
        payload, headers = self._request(
            "POST",
            "/api/motions/local/exchange",
            {"handoffToken": handoff_token},
            authenticated=False,
            expected_status=200,
        )
        try:
            body = _exact_object(payload, {"sessionId", "expiresAt"})
            session_id = body["sessionId"]
            expires_at = body["expiresAt"]
            access_token = _header(headers, "X-Artoke-Local-Access")
            if any(not isinstance(item, str) or not item for item in (session_id, expires_at, access_token)):
                raise TypeError
            session_id = _canonical_job_id(session_id)
            expires_at = _valid_rfc3339_timestamp(expires_at)
            if not re.fullmatch(rf"{re.escape(session_id)}\.[0-9a-f]{{64}}", access_token):
                raise TypeError
        except (KeyError, TypeError, ValueError, LocalIngestApiError):
            raise LocalIngestApiError("ARTOKE exchange response is invalid") from None
        self._access_token = access_token
        return LocalSession(session_id=session_id, expires_at=expires_at)

    def create_job(
        self,
        metadata: Mapping[str, object],
    ) -> LocalJob:
        if self._job_id is not None:
            raise LocalIngestApiError("Local motion session already has a job")
        payload = self._validate_source_metadata(metadata)
        response, _ = self._request(
            "POST",
            "/api/motions/local/jobs",
            payload,
            expected_status=201,
        )
        job = self._parse_job(response, None)
        self._job_id = job.job_id
        return job

    def report_progress(self, job_id: str, stage: str, progress: int) -> ProgressResult:
        bound = self._require_job(job_id)
        if stage not in _PROGRESS_STAGES or not _is_int(progress) or not 0 <= progress <= 100:
            raise LocalIngestApiError("Local motion progress is invalid")
        response, _ = self._request(
            "POST",
            f"/api/motions/local/jobs/{quote(bound, safe='')}/progress",
            {"stage": stage, "progress": progress},
        )
        try:
            body = _exact_object(response, {"cancelRequested"})
            if not isinstance(body["cancelRequested"], bool):
                raise TypeError
            return ProgressResult(cancel_requested=body["cancelRequested"])
        except (KeyError, TypeError):
            raise LocalIngestApiError("ARTOKE progress response is invalid") from None

    def authorize_uploads(self, job_id: str) -> tuple[UploadAuthorization, ...]:
        bound = self._require_job(job_id)
        response, _ = self._request(
            "POST",
            f"/api/motions/local/jobs/{quote(bound, safe='')}/uploads",
            None,
        )
        try:
            body = _exact_object(response, {"uploads"})
            items = body["uploads"]
            if not isinstance(items, list) or len(items) != len(_ARTIFACT_KINDS):
                raise TypeError
            uploads: list[UploadAuthorization] = []
            kinds: set[str] = set()
            for raw in items:
                item = _exact_object(raw, {"kind", "uploadUrl"})
                kind = item["kind"]
                upload_url = item["uploadUrl"]
                if (
                    kind not in _ARTIFACT_KINDS
                    or kind in kinds
                    or not isinstance(upload_url, str)
                    or not upload_url
                ):
                    raise TypeError
                kinds.add(kind)
                uploads.append(UploadAuthorization(kind=kind, upload_url=upload_url))
            return tuple(uploads)
        except (KeyError, TypeError):
            raise LocalIngestApiError("ARTOKE upload response is invalid") from None

    def authorize_source_upload(self, job_id: str) -> SourceUploadAuthorization:
        bound = self._require_job(job_id)
        response, _ = self._request(
            "POST",
            f"/api/motions/local/jobs/{quote(bound, safe='')}/source",
            None,
        )
        try:
            body = _exact_object(response, {"uploadUrl"})
            upload_url = body["uploadUrl"]
            if not isinstance(upload_url, str) or not upload_url:
                raise TypeError
            return SourceUploadAuthorization(upload_url)
        except (KeyError, TypeError):
            raise LocalIngestApiError("ARTOKE source upload response is invalid") from None

    def complete_source_upload(self, job_id: str) -> LocalJob:
        bound = self._require_job(job_id)
        response, _ = self._request(
            "POST",
            f"/api/motions/local/jobs/{quote(bound, safe='')}/source/complete",
            None,
        )
        return self._parse_job(response, bound)

    def publish(
        self,
        job_id: str,
        edit_revision: int,
        artifacts: Sequence[Mapping[str, object]],
    ) -> LocalJob:
        bound = self._require_job(job_id)
        if not _is_int(edit_revision) or not 0 <= edit_revision <= _MAX_EDIT_REVISION:
            raise LocalIngestApiError("Local motion publication is invalid")
        safe_artifacts = [self._validate_artifact(item) for item in artifacts]
        if (
            len(safe_artifacts) != len(_ARTIFACT_KINDS)
            or {item["kind"] for item in safe_artifacts} != _ARTIFACT_KINDS
        ):
            raise LocalIngestApiError("Local motion publication is invalid")
        response, _ = self._request(
            "POST",
            f"/api/motions/local/jobs/{quote(bound, safe='')}/publish",
            {"editRevision": edit_revision, "artifacts": safe_artifacts},
        )
        return self._parse_job(response, bound)

    def finish_failed(self, job_id: str, error_code: str) -> LocalJob:
        if not isinstance(error_code, str) or not _SAFE_ERROR_CODE.fullmatch(error_code):
            raise LocalIngestApiError("Local motion terminal state is invalid")
        return self._finish(job_id, {"status": "failed", "errorCode": error_code})

    def finish_cancelled(self, job_id: str) -> LocalJob:
        return self._finish(job_id, {"status": "cancelled"})

    def acknowledge_cleanup(self, job_id: str) -> bool:
        bound = self._require_job(job_id)
        response, _ = self._request(
            "POST",
            f"/api/motions/local/jobs/{quote(bound, safe='')}/cleanup",
            {},
        )
        try:
            body = _exact_object(response, {"acknowledged"})
            if not isinstance(body["acknowledged"], bool):
                raise TypeError
            return body["acknowledged"]
        except (KeyError, TypeError):
            raise LocalIngestApiError("ARTOKE cleanup response is invalid") from None

    def _finish(self, job_id: str, payload: dict[str, object]) -> LocalJob:
        bound = self._require_job(job_id)
        response, _ = self._request(
            "POST",
            f"/api/motions/local/jobs/{quote(bound, safe='')}/terminal",
            payload,
        )
        return self._parse_job(response, bound)

    def _require_job(self, job_id: str) -> str:
        candidate = _canonical_job_id(job_id)
        if self._job_id is None or candidate != self._job_id:
            raise LocalIngestApiError("Local motion request targets a different job")
        return candidate

    def _request(
        self,
        method: str,
        path: str,
        payload: object | None,
        *,
        authenticated: bool = True,
        expected_status: int = 200,
    ) -> tuple[object, object]:
        if self._cancelled():
            raise LocalIngestCancelled()
        headers = {"Accept": "application/json"}
        if authenticated:
            if self._access_token is None:
                raise LocalIngestApiError("Local motion session is not authenticated")
            headers["Authorization"] = f"Bearer {self._access_token}"
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}",
            method=method,
            data=data,
            headers=headers,
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                if response.status != expected_status:
                    raise LocalIngestApiError(
                        f"ARTOKE API returned HTTP {response.status}",
                        status=response.status,
                    )
                body = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise LocalIngestApiError("ARTOKE API response is invalid")
                try:
                    decoded = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    raise LocalIngestApiError("ARTOKE API response is invalid") from None
                return decoded, response.headers
        except LocalIngestApiError:
            raise
        except HTTPError as exc:
            raise LocalIngestApiError(
                f"ARTOKE API returned HTTP {exc.code}",
                status=exc.code,
            ) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise LocalIngestApiError("ARTOKE API request failed") from None

    @staticmethod
    def _validate_source_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
        try:
            body = _exact_object(dict(metadata), _SOURCE_KEYS)
            if any(not isinstance(body[key], str) or not body[key] for key in ("name", "filename", "contentType")):
                raise TypeError
            name = body["name"]
            filename = body["filename"]
            content_type = body["contentType"]
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
                raise TypeError
            if (
                not isinstance(filename, str)
                or len(filename) > 180
                or filename in {".", ".."}
                or "/" in filename
                or "\\" in filename
                or content_type not in _VIDEO_EXTENSION
                or not filename.lower().endswith(_VIDEO_EXTENSION[content_type])
            ):
                raise TypeError
            if (
                not _is_int(body["sizeBytes"])
                or not 0 < body["sizeBytes"] <= _MAX_SOURCE_BYTES
            ):
                raise TypeError
            _bounded_float(body["durationSeconds"], 0.0, _MAX_DURATION_SECONDS)
            if not isinstance(body["sourceSha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", body["sourceSha256"]):
                raise TypeError
            return body
        except (KeyError, TypeError, ValueError):
            raise LocalIngestApiError("Local motion source metadata is invalid") from None

    @staticmethod
    def _validate_artifact(artifact: Mapping[str, object]) -> dict[str, object]:
        try:
            body = _exact_object(dict(artifact), _ARTIFACT_KEYS)
            if body["kind"] not in _ARTIFACT_KINDS:
                raise TypeError
            kind = body["kind"]
            if (
                not _is_int(body["sizeBytes"])
                or not 0 < body["sizeBytes"] <= _ARTIFACT_SIZE_LIMITS[kind]
            ):
                raise TypeError
            if not isinstance(body["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", body["sha256"]):
                raise TypeError
            if (
                not isinstance(body["formatVersion"], str)
                or not 1 <= len(body["formatVersion"]) <= 40
            ):
                raise TypeError
            return body
        except (KeyError, TypeError, ValueError):
            raise LocalIngestApiError("Local motion artifact metadata is invalid") from None

    @staticmethod
    def _parse_job(payload: object, expected_job_id: str | None) -> LocalJob:
        keys = {
            "id",
            "name",
            "status",
            "sourceFilename",
            "sourceDurationSeconds",
            "sourceSizeBytes",
            "progress",
            "sourceDeleteAfter",
            "sourceDeletedAt",
            "progressStage",
            "errorCode",
            "errorMessage",
            "createdAt",
            "completedAt",
        }
        try:
            body = _exact_object(payload, keys)
            job_id = _canonical_job_id(body["id"])
            if expected_job_id is not None and job_id != expected_job_id:
                raise TypeError
            required_strings = (body["name"], body["sourceFilename"], body["createdAt"])
            if any(not isinstance(value, str) or not value for value in required_strings):
                raise TypeError
            if body["name"] != body["name"].strip() or len(body["name"]) > 120:
                raise TypeError
            if (
                len(body["sourceFilename"]) > 180
                or body["sourceFilename"] in {".", ".."}
                or "/" in body["sourceFilename"]
                or "\\" in body["sourceFilename"]
            ):
                raise TypeError
            if body["status"] not in _JOB_STATUSES:
                raise TypeError
            duration = body["sourceDurationSeconds"]
            duration_value = (
                None
                if duration is None
                else _bounded_float(duration, 0.0, _MAX_DURATION_SECONDS)
            )
            if (
                not _is_int(body["sourceSizeBytes"])
                or not 0 < body["sourceSizeBytes"] <= _MAX_SOURCE_BYTES
            ):
                raise TypeError
            if not _is_int(body["progress"]) or not 0 <= body["progress"] <= 100:
                raise TypeError
            nullable_strings = (
                body["sourceDeleteAfter"],
                body["sourceDeletedAt"],
                body["progressStage"],
                body["errorCode"],
                body["errorMessage"],
                body["completedAt"],
            )
            if any(value is not None and not isinstance(value, str) for value in nullable_strings):
                raise TypeError
            if body["progressStage"] is not None and body["progressStage"] not in _JOB_PROGRESS_STAGES:
                raise TypeError
            if body["errorCode"] is not None and not _SAFE_ERROR_CODE.fullmatch(body["errorCode"]):
                raise TypeError
            return LocalJob(
                job_id=job_id,
                name=body["name"],
                status=body["status"],
                source_filename=body["sourceFilename"],
                source_duration_seconds=duration_value,
                source_size_bytes=body["sourceSizeBytes"],
                progress=body["progress"],
                source_delete_after=body["sourceDeleteAfter"],
                source_deleted_at=body["sourceDeletedAt"],
                progress_stage=body["progressStage"],
                error_code=body["errorCode"],
                error_message=body["errorMessage"],
                created_at=body["createdAt"],
                completed_at=body["completedAt"],
            )
        except (KeyError, TypeError, ValueError, OverflowError, LocalIngestApiError):
            raise LocalIngestApiError("ARTOKE job response is invalid") from None
