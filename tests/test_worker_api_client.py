import json
from urllib.error import HTTPError

import pytest

from src.worker.api_client import ArtokeApiClient, WorkerApiError


class Response:
    def __init__(self, status: int, payload: object | None = None) -> None:
        self.status = status
        self._body = b"" if payload is None else json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


def test_claim_sends_bearer_token_and_parses_job() -> None:
    requests = []

    def open_request(request, timeout):
        requests.append((request, timeout))
        return Response(200, {
            "job": {"id": "job-1", "sourceFilename": "walk.mp4"},
            "source": {"objectPath": "owner/job/source/walk.mp4", "downloadUrl": "https://signed"},
        })

    client = ArtokeApiClient("https://artoke.com", "secret-token", opener=open_request)
    claim = client.claim()

    assert claim is not None
    assert claim.job_id == "job-1"
    request, timeout = requests[0]
    assert request.full_url == "https://artoke.com/api/motions/worker/claim"
    assert request.headers["Authorization"] == "Bearer secret-token"
    assert json.loads(request.data) == {"capabilities": {"rtmw3d": True}}
    assert timeout == 30


def test_claim_returns_none_for_empty_queue() -> None:
    client = ArtokeApiClient(
        "https://artoke.com",
        "secret-token",
        opener=lambda *_args, **_kwargs: Response(204),
    )
    assert client.claim() is None


def test_client_sends_heartbeat_and_terminal_payloads() -> None:
    payloads = []

    def open_request(request, timeout):
        payloads.append((request.full_url, json.loads(request.data or b"{}")))
        if request.full_url.endswith("/heartbeat"):
            return Response(200, {"cancelRequested": False, "leaseExpiresAt": "later"})
        return Response(200, {"status": "failed"})

    client = ArtokeApiClient("https://artoke.com/", "token", opener=open_request)
    heartbeat = client.heartbeat("job-1", "extracting", 35)
    client.finish_failed("job-1", "rtmw3d_failed")
    client.finish_cancelled("job-1")

    assert heartbeat.cancel_requested is False
    assert payloads[0][1] == {"stage": "extracting", "progress": 35}
    assert payloads[1][1] == {"status": "failed", "errorCode": "rtmw3d_failed"}
    assert payloads[2][1] == {"status": "cancelled"}


def test_client_authorizes_uploads_and_publishes_manifest() -> None:
    requests = []

    def open_request(request, timeout):
        requests.append((request.full_url, json.loads(request.data or b"{}")))
        if request.full_url.endswith("/uploads"):
            return Response(200, {"uploads": [{
                "kind": "bvh",
                "objectPath": "owner/job/result/motion.bvh",
                "token": "signed",
                "signedUrl": "https://storage.test/upload?token=signed",
            }]})
        return Response(200, {"status": "completed"})

    client = ArtokeApiClient("https://artoke.com", "token", opener=open_request)
    uploads = client.authorize_uploads("job-1")
    manifest = [{
        "kind": "bvh",
        "objectPath": uploads[0].object_path,
        "sizeBytes": 10,
        "sha256": "a" * 64,
        "formatVersion": "1",
    }]
    client.publish("job-1", manifest)

    assert uploads[0].token == "signed"
    assert uploads[0].signed_url == "https://storage.test/upload?token=signed"
    assert requests[1][0].endswith("/api/motions/worker/artifacts")
    assert requests[1][1] == {"jobId": "job-1", "artifacts": manifest}


def test_error_never_contains_token_or_signed_query() -> None:
    token = "super-secret-token"
    signed = "https://storage.test/file?token=signed-secret"

    def fail(request, timeout):
        raise HTTPError(request.full_url, 503, signed, {}, None)

    client = ArtokeApiClient("https://artoke.com", token, opener=fail)
    with pytest.raises(WorkerApiError) as raised:
        client.claim()

    message = str(raised.value)
    assert token not in message
    assert "signed-secret" not in message
    assert "503" in message
