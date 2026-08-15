from __future__ import annotations

import io
import json
from urllib.error import HTTPError, URLError

import pytest

from maxmcp.local_ingest.api_client import (
    LocalIngestApiClient,
    LocalIngestApiError,
    LocalIngestCancelled,
)


JOB_ID = "00000000-0000-4000-8000-000000000002"
OTHER_JOB_ID = "00000000-0000-4000-8000-000000000003"
HANDOFF = "a" * 64
SESSION_ID = "00000000-0000-4000-8000-000000000001"
EXPIRES_AT = "2026-08-16T00:05:00.000Z"
ACCESS = f"{SESSION_ID}.{'e' * 64}"


def job_payload(*, job_id: str = JOB_ID, status: str = "processing") -> dict[str, object]:
    return {
        "id": job_id,
        "name": "Walk",
        "status": status,
        "sourceFilename": "walk.mp4",
        "sourceDurationSeconds": 4.2,
        "sourceSizeBytes": 100,
        "progress": 25,
        "sourceDeleteAfter": "2026-08-16T01:00:00.000Z",
        "sourceDeletedAt": None,
        "progressStage": "extracting",
        "errorCode": None,
        "errorMessage": None,
        "createdAt": "2026-08-16T00:00:00.000Z",
        "completedAt": None,
    }


def source_metadata() -> dict[str, object]:
    return {
        "name": "Walk",
        "filename": "walk.mp4",
        "contentType": "video/mp4",
        "sizeBytes": 100,
        "durationSeconds": 4.2,
        "sourceSha256": "b" * 64,
    }


def artifact_manifest() -> list[dict[str, object]]:
    return [
        {"kind": kind, "sizeBytes": index + 1, "sha256": chr(99 + index) * 64, "formatVersion": "1"}
        for index, kind in enumerate(("bvh", "rtmw3d_json", "thumbnail", "metadata"))
    ]


class Response:
    def __init__(
        self,
        status: int,
        payload: object | None = None,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = (
            body
            if body is not None
            else (b"" if payload is None else json.dumps(payload).encode("utf-8"))
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]


def exchanged_client(opener, *, cancelled=lambda: False) -> LocalIngestApiClient:
    client = LocalIngestApiClient(
        "https://artoke.com",
        opener=opener,
        cancelled=cancelled,
    )
    client.exchange(HANDOFF)
    return client


@pytest.mark.parametrize(
    "base_url",
    [
        "http://artoke.com",
        "http://192.168.0.10:3000",
        "http://10.0.0.2",
        "ftp://artoke.com",
        "https://user:password@artoke.com",
        "https://artoke.com?token=secret",
        "https://artoke.com/#fragment",
    ],
)
def test_client_rejects_unsafe_server_urls(base_url: str) -> None:
    with pytest.raises(ValueError):
        LocalIngestApiClient(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://artoke.com",
        "http://localhost:3000",
        "http://127.0.0.1:3000/",
        "http://[::1]:3000",
    ],
)
def test_client_allows_https_and_loopback_http(base_url: str) -> None:
    LocalIngestApiClient(base_url)


def test_exchange_reads_access_only_from_header_and_does_not_send_authorization() -> None:
    requests = []

    def open_request(request, timeout):
        requests.append((request, timeout))
        return Response(
            200,
            {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT},
            headers={"X-Artoke-Local-Access": ACCESS},
        )

    client = LocalIngestApiClient("https://artoke.com/", opener=open_request)
    session = client.exchange(HANDOFF)

    assert session.session_id == SESSION_ID
    assert session.expires_at == EXPIRES_AT
    request, timeout = requests[0]
    assert request.full_url == "https://artoke.com/api/motions/local/exchange"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") is None
    assert json.loads(request.data) == {"handoffToken": HANDOFF}
    assert timeout == 15.0
    assert HANDOFF not in repr(client)
    assert ACCESS not in repr(client)
    assert SESSION_ID not in repr(session)
    assert EXPIRES_AT not in repr(session)


@pytest.mark.parametrize(
    "payload,headers",
    [
        (
            {
                "sessionId": SESSION_ID,
                "expiresAt": EXPIRES_AT,
                "accessToken": ACCESS,
            },
            {},
        ),
        (
            {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT},
            {},
        ),
        ([], {"X-Artoke-Local-Access": ACCESS}),
        (
            {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT},
            {"X-Artoke-Local-Access": "/home/me?token=query-secret"},
        ),
    ],
)
def test_exchange_rejects_body_token_missing_header_and_wrong_shape(payload, headers) -> None:
    client = LocalIngestApiClient(
        "https://artoke.com",
        opener=lambda *_args, **_kwargs: Response(200, payload, headers=headers),
    )

    with pytest.raises(LocalIngestApiError, match="exchange response is invalid"):
        client.exchange(HANDOFF)


@pytest.mark.parametrize(
    "session_id,expires_at",
    [
        ("not-a-uuid", EXPIRES_AT),
        ("/home/me/session", EXPIRES_AT),
        (SESSION_ID, "2026-08-16T00:05:00"),
        (SESSION_ID, "not-a-time?token=query-secret"),
        (SESSION_ID, "2" * 129),
    ],
)
def test_exchange_rejects_malicious_or_unbounded_session_scalars(session_id, expires_at) -> None:
    client = LocalIngestApiClient(
        "https://artoke.com",
        opener=lambda *_a, **_k: Response(
            200,
            {"sessionId": session_id, "expiresAt": expires_at},
            headers={"X-Artoke-Local-Access": ACCESS},
        ),
    )
    with pytest.raises(LocalIngestApiError, match="exchange response is invalid") as raised:
        client.exchange(HANDOFF)
    rendered = f"{raised.value!s} {raised.value!r}"
    assert str(session_id) not in rendered
    assert str(expires_at) not in rendered


def test_create_job_sends_scoped_bearer_and_binds_the_returned_job() -> None:
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        if request.full_url.endswith("/exchange"):
            return Response(
                200,
                {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT},
                headers={"X-Artoke-Local-Access": ACCESS},
            )
        return Response(201, job_payload())

    client = exchanged_client(open_request)
    metadata = source_metadata()
    job = client.create_job(metadata)

    assert job.job_id == JOB_ID
    request = requests[1]
    assert request.full_url == "https://artoke.com/api/motions/local/jobs"
    assert request.get_header("Authorization") == f"Bearer {ACCESS}"
    assert json.loads(request.data) == metadata


def test_progress_upload_publish_terminal_and_cleanup_use_only_task4_routes() -> None:
    requests = []

    def open_request(request, timeout):
        requests.append(request)
        if request.full_url.endswith("/exchange"):
            return Response(
                200,
                {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT},
                headers={"X-Artoke-Local-Access": ACCESS},
            )
        if request.full_url.endswith("/jobs"):
            return Response(201, job_payload())
        if request.full_url.endswith("/progress"):
            return Response(200, {"cancelRequested": False})
        if request.full_url.endswith("/uploads"):
            return Response(
                200,
                {
                    "uploads": [
                        {"kind": kind, "uploadUrl": f"https://storage.test/{kind}?token=signed"}
                        for kind in ("bvh", "rtmw3d_json", "thumbnail", "metadata")
                    ]
                },
            )
        if request.full_url.endswith("/cleanup"):
            return Response(200, {"acknowledged": True})
        return Response(200, job_payload(status="completed"))

    client = exchanged_client(open_request)
    client.create_job(source_metadata())
    assert client.report_progress(JOB_ID, "extracting", 25).cancel_requested is False
    uploads = client.authorize_uploads(JOB_ID)
    assert uploads[0].kind == "bvh"
    assert uploads[0].upload_url == "https://storage.test/bvh?token=signed"
    assert "signed" not in repr(uploads[0])
    artifacts = artifact_manifest()
    assert client.publish(JOB_ID, 0, artifacts).status == "completed"
    assert client.finish_failed(JOB_ID, "rtmw3d_failed").status == "completed"
    assert client.finish_cancelled(JOB_ID).status == "completed"
    assert client.acknowledge_cleanup(JOB_ID) is True

    assert [request.full_url.removeprefix("https://artoke.com") for request in requests] == [
        "/api/motions/local/exchange",
        "/api/motions/local/jobs",
        f"/api/motions/local/jobs/{JOB_ID}/progress",
        f"/api/motions/local/jobs/{JOB_ID}/uploads",
        f"/api/motions/local/jobs/{JOB_ID}/publish",
        f"/api/motions/local/jobs/{JOB_ID}/terminal",
        f"/api/motions/local/jobs/{JOB_ID}/terminal",
        f"/api/motions/local/jobs/{JOB_ID}/cleanup",
    ]
    assert json.loads(requests[2].data) == {"stage": "extracting", "progress": 25}
    assert requests[3].data is None
    assert json.loads(requests[4].data) == {"editRevision": 0, "artifacts": artifacts}
    assert json.loads(requests[5].data) == {"status": "failed", "errorCode": "rtmw3d_failed"}
    assert json.loads(requests[6].data) == {"status": "cancelled"}
    assert json.loads(requests[7].data) == {}
    assert not hasattr(client, "fetch_job")


def test_client_rejects_cross_job_calls_before_network_request() -> None:
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/exchange"):
            return Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS})
        return Response(201, job_payload())

    client = exchanged_client(open_request)
    client.create_job(source_metadata())

    with pytest.raises(LocalIngestApiError, match="different job"):
        client.report_progress(OTHER_JOB_ID, "extracting", 20)
    assert len(calls) == 2


def test_client_rejects_cross_job_response() -> None:
    calls = 0

    def open_request(request, timeout):
        nonlocal calls
        calls += 1
        if request.full_url.endswith("/exchange"):
            return Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS})
        if calls == 2:
            return Response(201, job_payload())
        return Response(200, job_payload(job_id=OTHER_JOB_ID, status="completed"))

    client = exchanged_client(open_request)
    client.create_job(source_metadata())
    with pytest.raises(LocalIngestApiError, match="job response is invalid"):
        client.publish(JOB_ID, 0, artifact_manifest())


def test_cooperative_cancellation_stops_subsequent_requests() -> None:
    cancelled = False
    calls = []

    def is_cancelled():
        return cancelled

    def open_request(request, timeout):
        nonlocal cancelled
        calls.append(request)
        cancelled = True
        return Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS})

    client = LocalIngestApiClient("https://artoke.com", opener=open_request, cancelled=is_cancelled)
    client.exchange(HANDOFF)

    with pytest.raises(LocalIngestCancelled):
        client.create_job(source_metadata())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "timeout",
    [10**10_000, float("inf"), float("nan"), True, "15"],
    ids=["huge-int", "inf", "nan", "bool", "string"],
)
def test_invalid_timeout_values_are_normalized_without_numeric_overflow(timeout) -> None:
    with pytest.raises(LocalIngestApiError, match="timeout is invalid"):
        LocalIngestApiClient("https://artoke.com", timeout=timeout)


@pytest.mark.parametrize("error", [TimeoutError("late"), URLError("offline")])
def test_network_failures_are_normalized_without_raw_details(error: Exception) -> None:
    secret_path = "C:\\Users\\me\\secret.mp4"

    def fail(*_args, **_kwargs):
        raise error

    client = LocalIngestApiClient("https://artoke.com", opener=fail)
    with pytest.raises(LocalIngestApiError, match="request failed") as raised:
        client.exchange(HANDOFF)
    message = f"{raised.value!s} {raised.value!r}"
    assert HANDOFF not in message
    assert secret_path not in message
    assert "offline" not in message


def test_http_error_redacts_query_access_handoff_body_and_paths() -> None:
    raw = b'{"error":"C:\\\\Users\\\\me\\\\secret.mp4 ' + HANDOFF.encode() + b'"}'

    def fail(request, timeout):
        raise HTTPError(
            request.full_url + "?token=query-secret",
            503,
            f"Bearer {ACCESS} /home/me/private.mp4",
            {},
            io.BytesIO(raw),
        )

    client = LocalIngestApiClient("https://artoke.com", opener=fail)
    with pytest.raises(LocalIngestApiError) as raised:
        client.exchange(HANDOFF)
    message = f"{raised.value!s} {raised.value!r}"
    for secret in (HANDOFF, ACCESS, "query-secret", "secret.mp4", "/home/me"):
        assert secret not in message
    assert raised.value.status == 503


def test_redirect_is_not_followed_and_is_reported_by_status() -> None:
    client = LocalIngestApiClient(
        "https://artoke.com",
        opener=lambda request, timeout: (_ for _ in ()).throw(
            HTTPError(request.full_url, 302, "https://evil.test/?token=secret", {}, None)
        ),
    )
    with pytest.raises(LocalIngestApiError) as raised:
        client.exchange(HANDOFF)
    assert raised.value.status == 302
    assert "evil" not in str(raised.value)


@pytest.mark.parametrize(
    "response",
    [
        Response(200, []),
        Response(200, body=b"<html>bad gateway</html>"),
        Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT, "unexpected": True}, headers={"X-Artoke-Local-Access": ACCESS}),
        Response(200, body=b"{" + b"x" * (1024 * 1024 + 1)),
    ],
)
def test_exchange_rejects_malformed_unexpected_and_oversized_responses(response) -> None:
    client = LocalIngestApiClient("https://artoke.com", opener=lambda *_a, **_k: response)
    with pytest.raises(LocalIngestApiError, match="response is invalid"):
        client.exchange(HANDOFF)


def test_job_and_upload_response_shapes_are_strict() -> None:
    responses = iter(
        [
            Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS}),
            Response(201, job_payload() | {"internalPath": "C:\\secret"}),
        ]
    )
    client = LocalIngestApiClient("https://artoke.com", opener=lambda *_a, **_k: next(responses))
    client.exchange(HANDOFF)
    with pytest.raises(LocalIngestApiError, match="job response is invalid"):
        client.create_job(source_metadata())


def test_create_job_rejects_local_path_metadata_before_request() -> None:
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        return Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS})

    client = exchanged_client(open_request)
    with pytest.raises(LocalIngestApiError, match="source metadata is invalid"):
        client.create_job(source_metadata() | {"localPath": "C:\\private\\walk.mp4"})
    assert len(calls) == 1


@pytest.mark.parametrize(
    "replacement",
    [
        {"filename": "C:\\private\\walk.mp4"},
        {"filename": "walk.mov"},
        {"contentType": "application/octet-stream"},
        {"sizeBytes": 2_147_483_649},
        {"durationSeconds": 300.001},
        {"name": "   "},
    ],
)
def test_create_job_rejects_unsafe_or_over_limit_metadata(replacement) -> None:
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        return Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS})

    client = exchanged_client(open_request)
    with pytest.raises(LocalIngestApiError, match="source metadata is invalid"):
        client.create_job(source_metadata() | replacement)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "duration",
    [10**10_000, float("inf"), float("nan"), True, "4.2"],
    ids=["huge-int", "inf", "nan", "bool", "string"],
)
def test_source_duration_numeric_failures_are_safe_and_never_reach_network(duration) -> None:
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        return Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS})

    client = exchanged_client(open_request)
    with pytest.raises(LocalIngestApiError, match="source metadata is invalid"):
        client.create_job(source_metadata() | {"durationSeconds": duration})
    assert len(calls) == 1


@pytest.mark.parametrize(
    "replacement",
    [
        {"sourceDurationSeconds": float("inf")},
        {"sourceDurationSeconds": float("nan")},
        {"sourceDurationSeconds": True},
        {"sourceSizeBytes": True},
        {"sourceSizeBytes": 2_147_483_649},
        {"progress": True},
        {"progress": 101},
    ],
    ids=[
        "duration-inf",
        "duration-nan",
        "duration-bool",
        "size-bool",
        "size-over-limit",
        "progress-bool",
        "progress-over-limit",
    ],
)
def test_job_numeric_failures_are_normalized_as_protocol_errors(replacement) -> None:
    responses = iter(
        [
            Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS}),
            Response(201, job_payload() | replacement),
        ]
    )
    client = LocalIngestApiClient("https://artoke.com", opener=lambda *_a, **_k: next(responses))
    client.exchange(HANDOFF)
    with pytest.raises(LocalIngestApiError, match="job response is invalid"):
        client.create_job(source_metadata())


@pytest.mark.parametrize(
    "replacement",
    [
        {"name": ""},
        {"name": "n" * 121},
        {"sourceFilename": "C:\\private\\walk.mp4"},
        {"sourceFilename": "f" * 177 + ".mp4"},
        {"progressStage": "/home/me/stage"},
    ],
)
def test_job_response_rejects_unbounded_or_path_like_server_scalars(replacement) -> None:
    responses = iter(
        [
            Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS}),
            Response(201, job_payload() | replacement),
        ]
    )
    client = LocalIngestApiClient("https://artoke.com", opener=lambda *_a, **_k: next(responses))
    client.exchange(HANDOFF)
    with pytest.raises(LocalIngestApiError, match="job response is invalid"):
        client.create_job(source_metadata())


def test_huge_integer_in_raw_json_response_is_normalized_without_parser_overflow() -> None:
    calls = 0

    def open_request(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS})
        body = json.dumps(job_payload()).replace("4.2", "9" * 10_000).encode("ascii")
        return Response(201, body=body)

    client = exchanged_client(open_request)
    with pytest.raises(LocalIngestApiError, match="response is invalid"):
        client.create_job(source_metadata())


def test_publish_rejects_incomplete_or_duplicate_artifact_sets_before_request() -> None:
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/exchange"):
            return Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS})
        return Response(201, job_payload())

    client = exchanged_client(open_request)
    client.create_job(source_metadata())
    invalid = artifact_manifest() + [artifact_manifest()[0]]
    with pytest.raises(LocalIngestApiError, match="publication is invalid"):
        client.publish(JOB_ID, 0, invalid)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "kind,limit",
    [
        ("bvh", 64 * 1024 * 1024),
        ("rtmw3d_json", 45 * 1024 * 1024),
        ("thumbnail", 5 * 1024 * 1024),
        ("metadata", 1024 * 1024),
    ],
)
def test_publish_accepts_each_exact_artifact_size_limit(kind: str, limit: int) -> None:
    def open_request(request, timeout):
        if request.full_url.endswith("/exchange"):
            return Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS})
        if request.full_url.endswith("/jobs"):
            return Response(201, job_payload())
        return Response(200, job_payload(status="completed"))

    client = exchanged_client(open_request)
    client.create_job(source_metadata())
    manifest = [item | ({"sizeBytes": limit} if item["kind"] == kind else {}) for item in artifact_manifest()]
    assert client.publish(JOB_ID, 0, manifest).status == "completed"


@pytest.mark.parametrize(
    "kind,limit",
    [
        ("bvh", 64 * 1024 * 1024),
        ("rtmw3d_json", 45 * 1024 * 1024),
        ("thumbnail", 5 * 1024 * 1024),
        ("metadata", 1024 * 1024),
    ],
)
def test_publish_rejects_each_artifact_size_limit_plus_one(kind: str, limit: int) -> None:
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/exchange"):
            return Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS})
        return Response(201, job_payload())

    client = exchanged_client(open_request)
    client.create_job(source_metadata())
    manifest = [item | ({"sizeBytes": limit + 1} if item["kind"] == kind else {}) for item in artifact_manifest()]
    with pytest.raises(LocalIngestApiError, match="artifact metadata is invalid"):
        client.publish(JOB_ID, 0, manifest)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "replacement",
    [
        {"sha256": "A" * 64},
        {"formatVersion": ""},
        {"formatVersion": "v" * 41},
    ],
)
def test_publish_rejects_non_server_compatible_artifact_scalars(replacement) -> None:
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        if request.full_url.endswith("/exchange"):
            return Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS})
        return Response(201, job_payload())

    client = exchanged_client(open_request)
    client.create_job(source_metadata())
    manifest = artifact_manifest()
    manifest[0] = manifest[0] | replacement
    with pytest.raises(LocalIngestApiError, match="artifact metadata is invalid"):
        client.publish(JOB_ID, 0, manifest)
    assert len(calls) == 2


def test_malformed_job_id_and_path_like_job_fields_are_safely_rejected() -> None:
    responses = iter(
        [
            Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS}),
            Response(201, job_payload(job_id="not-a-uuid")),
        ]
    )
    client = LocalIngestApiClient("https://artoke.com", opener=lambda *_a, **_k: next(responses))
    client.exchange(HANDOFF)
    with pytest.raises(LocalIngestApiError, match="job response is invalid"):
        client.create_job(source_metadata())

    safe_responses = iter(
        [
            Response(200, {"sessionId": SESSION_ID, "expiresAt": EXPIRES_AT}, headers={"X-Artoke-Local-Access": ACCESS}),
            Response(
                201,
                job_payload() | {
                    "sourceFilename": "C:\\private\\walk.mp4",
                    "errorMessage": "/home/me/private.mp4",
                },
            ),
        ]
    )
    safe_client = LocalIngestApiClient("https://artoke.com", opener=lambda *_a, **_k: next(safe_responses))
    safe_client.exchange(HANDOFF)
    with pytest.raises(LocalIngestApiError, match="job response is invalid") as raised:
        safe_client.create_job(source_metadata())
    rendered = f"{raised.value!s} {raised.value!r}"
    assert "C:\\private" not in rendered
    assert "/home/me" not in rendered
