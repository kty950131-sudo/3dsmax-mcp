from contextlib import contextmanager
from http.client import HTTPConnection, HTTPResponse
from pathlib import Path
import base64
import json
import threading
import time
from typing import Iterator

import pytest

from maxmcp.local_ingest.server import CompanionHTTPServer
from maxmcp.worker.workspace import JobWorkspace


JOB_ID = "00000000-0000-4000-8000-000000000009"


def encoded_name(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).rstrip(b"=").decode("ascii")


@contextmanager
def running_server(tmp_path: Path, **kwargs: object) -> Iterator[CompanionHTTPServer]:
    server = CompanionHTTPServer(tmp_path, JOB_ID, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.close()
        thread.join(timeout=2)


def request(
    server: CompanionHTTPServer,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPResponse:
    connection = HTTPConnection("127.0.0.1", server.port, timeout=2)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    response._artoke_connection = connection  # type: ignore[attr-defined]
    return response


def read_json(response: HTTPResponse) -> dict[str, object]:
    try:
        return json.loads(response.read())
    finally:
        response._artoke_connection.close()  # type: ignore[attr-defined]


def claim(server: CompanionHTTPServer) -> tuple[str, str]:
    response = request(server, "GET", "/")
    assert response.status == 200
    cookie = response.getheader("Set-Cookie")
    assert cookie is not None
    response.read()
    response._artoke_connection.close()  # type: ignore[attr-defined]
    cookie_pair = cookie.split(";", 1)[0]
    status = request(server, "GET", "/api/session", headers={"Cookie": cookie_pair})
    csrf = read_json(status)["csrfToken"]
    assert isinstance(csrf, str)
    return cookie_pair, csrf


def security_headers(response: HTTPResponse) -> None:
    assert response.getheader("Cache-Control") == "no-store"
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert response.getheader("X-Frame-Options") == "DENY"
    csp = response.getheader("Content-Security-Policy") or ""
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-src 'none'" in csp
    assert "unsafe-inline" not in csp


def test_server_binds_ipv4_loopback_on_ephemeral_port(tmp_path: Path) -> None:
    with running_server(tmp_path) as server:
        assert server.server_address[0] == "127.0.0.1"
        assert isinstance(server.port, int) and server.port > 0
        assert server.origin == f"http://127.0.0.1:{server.port}"


def test_shell_claims_one_browser_with_strict_http_only_cookie(tmp_path: Path) -> None:
    with running_server(tmp_path) as server:
        response = request(server, "GET", "/")
        assert response.status == 200
        security_headers(response)
        cookie = response.getheader("Set-Cookie") or ""
        html = response.read().decode("utf-8")
        response._artoke_connection.close()  # type: ignore[attr-defined]

        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        assert "Path=/" in cookie
        assert "Domain=" not in cookie and "Secure" not in cookie
        assert "영상 하나로 모션을 만드세요" in html
        assert "영상 선택하고 시작" in html
        assert "json" not in html.lower()

        second = request(server, "GET", "/")
        assert second.status == 409
        assert read_json(second) == {"error": "browser_session_in_use"}


def test_exact_host_and_same_origin_are_required(tmp_path: Path) -> None:
    with running_server(tmp_path) as server:
        bad_host = request(server, "GET", "/", headers={"Host": "localhost:9999"})
        assert bad_host.status == 403
        assert read_json(bad_host) == {"error": "invalid_authority"}

        cookie, csrf = claim(server)
        headers = {
            "Cookie": cookie,
            "X-CSRF-Token": csrf,
            "X-Artoke-Filename": encoded_name("clip.mp4"),
            "Origin": "https://evil.example",
            "Content-Type": "application/octet-stream",
        }
        bad_origin = request(server, "POST", "/api/source", body=b"video", headers=headers)
        assert bad_origin.status == 403
        assert read_json(bad_origin) == {"error": "invalid_origin"}


def test_static_assets_are_package_owned_and_all_routes_have_security_headers(tmp_path: Path) -> None:
    with running_server(tmp_path) as server:
        cookie, _ = claim(server)
        for path, content_type in (
            ("/app.js", "text/javascript"),
            ("/styles.css", "text/css"),
        ):
            response = request(server, "GET", path, headers={"Cookie": cookie})
            assert response.status == 200
            assert content_type in (response.getheader("Content-Type") or "")
            security_headers(response)
            assert response.read()
            response._artoke_connection.close()  # type: ignore[attr-defined]

        unknown = request(server, "GET", "/../../secret")
        assert unknown.status == 404
        security_headers(unknown)
        assert read_json(unknown) == {"error": "not_found"}


def test_upload_requires_cookie_csrf_and_exact_decimal_content_length(tmp_path: Path) -> None:
    with running_server(tmp_path) as server:
        cookie, csrf = claim(server)
        base = {
            "Origin": server.origin,
            "X-Artoke-Filename": encoded_name("clip.mp4"),
            "Content-Type": "application/octet-stream",
        }
        missing_session = request(server, "POST", "/api/source", body=b"video", headers=base)
        assert missing_session.status == 403
        assert read_json(missing_session) == {"error": "browser_session_required"}

        missing_csrf = request(
            server, "POST", "/api/source", body=b"video", headers={**base, "Cookie": cookie}
        )
        assert missing_csrf.status == 403
        assert read_json(missing_csrf) == {"error": "csrf_rejected"}

        connection = HTTPConnection("127.0.0.1", server.port, timeout=2)
        connection.putrequest("POST", "/api/source")
        for key, value in {**base, "Cookie": cookie, "X-CSRF-Token": csrf}.items():
            connection.putheader(key, value)
        connection.endheaders()
        missing_length = connection.getresponse()
        assert missing_length.status == 411
        assert json.loads(missing_length.read()) == {"error": "content_length_required"}
        connection.close()


def test_upload_rejects_non_raw_media_type_before_write(tmp_path: Path) -> None:
    with running_server(tmp_path) as server:
        cookie, csrf = claim(server)
        response = request(
            server,
            "POST",
            "/api/source",
            body=b"video",
            headers={
                "Cookie": cookie,
                "X-CSRF-Token": csrf,
                "Origin": server.origin,
                "X-Artoke-Filename": "clip.mp4",
                "Content-Type": "multipart/form-data; boundary=unsafe",
            },
        )

        assert response.status == 415
        assert read_json(response) == {"error": "raw_source_required"}
        assert list(server.session.workspace.path.iterdir()) == []


def test_upload_streams_raw_bytes_and_returns_no_local_path_or_filename(tmp_path: Path) -> None:
    with running_server(tmp_path, chunk_size=3) as server:
        cookie, csrf = claim(server)
        response = request(
            server,
            "POST",
            "/api/source",
            body=b"video-bytes",
            headers={
                "Cookie": cookie,
                "X-CSRF-Token": csrf,
                "Origin": server.origin,
                "X-Artoke-Filename": encoded_name("private clip.mp4"),
                "Content-Type": "application/octet-stream",
            },
        )

        assert response.status == 201
        payload = read_json(response)
        assert payload == {"status": "source_received", "sizeBytes": 11}
        assert "private" not in json.dumps(payload)
        files = list(server.session.workspace.path.iterdir())
        assert len(files) == 1 and files[0].read_bytes() == b"video-bytes"


def test_oversize_upload_is_rejected_before_write(tmp_path: Path) -> None:
    with running_server(tmp_path, max_source_bytes=4) as server:
        cookie, csrf = claim(server)
        response = request(
            server,
            "POST",
            "/api/source",
            body=b"12345",
            headers={
                "Cookie": cookie,
                "X-CSRF-Token": csrf,
                "Origin": server.origin,
                "X-Artoke-Filename": encoded_name("clip.mp4"),
                "Content-Type": "application/octet-stream",
            },
        )

        assert response.status == 413
        assert read_json(response) == {"error": "source_too_large"}
        assert list(server.session.workspace.path.iterdir()) == []


def test_cancel_is_idempotent_and_removes_workspace(tmp_path: Path) -> None:
    with running_server(tmp_path) as server:
        cookie, csrf = claim(server)
        headers = {"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": server.origin}

        response = request(server, "POST", "/api/cancel", body=b"", headers=headers)
        assert response.status == 200
        assert read_json(response) == {"status": "cancelled", "cleaned": True}
        assert not server.session.workspace.path.exists()

        response = request(server, "POST", "/api/cancel", body=b"", headers=headers)
        assert response.status == 200
        assert read_json(response) == {"status": "cancelled", "cleaned": True}


def test_unknown_method_returns_safe_405_without_reflection(tmp_path: Path) -> None:
    with running_server(tmp_path) as server:
        response = request(server, "PUT", "/api/source?secret=value", body=b"ignored")
        assert response.status == 405
        security_headers(response)
        assert read_json(response) == {"error": "method_not_allowed"}


def test_korean_display_filename_uses_strict_urlsafe_encoding(tmp_path: Path) -> None:
    with running_server(tmp_path) as server:
        cookie, csrf = claim(server)
        response = request(
            server,
            "POST",
            "/api/source",
            body=b"video",
            headers={
                "Cookie": cookie,
                "X-CSRF-Token": csrf,
                "Origin": server.origin,
                "X-Artoke-Filename": encoded_name("테스트 영상.mp4"),
                "Content-Type": "application/octet-stream",
            },
        )

        assert response.status == 201
        assert read_json(response) == {"status": "source_received", "sizeBytes": 5}
        assert server.session.display_name == "테스트 영상.mp4"


@pytest.mark.parametrize("encoded", ["%%%", "YQ=", "a" * 1025])
def test_malformed_or_oversize_encoded_filename_is_rejected_before_write(
    tmp_path: Path,
    encoded: str,
) -> None:
    with running_server(tmp_path) as server:
        cookie, csrf = claim(server)
        response = request(
            server,
            "POST",
            "/api/source",
            body=b"video",
            headers={
                "Cookie": cookie,
                "X-CSRF-Token": csrf,
                "Origin": server.origin,
                "X-Artoke-Filename": encoded,
                "Content-Type": "application/octet-stream",
            },
        )

        assert response.status == 400
        assert read_json(response) == {"error": "invalid_display_name"}
        assert list(server.session.workspace.path.iterdir()) == []


def test_close_before_serve_never_waits_for_shutdown_loop(tmp_path: Path) -> None:
    server = CompanionHTTPServer(tmp_path, JOB_ID)
    workspace = server.session.workspace.path
    result: list[object] = []
    thread = threading.Thread(target=lambda: result.append(server.close()), daemon=True)

    started = time.monotonic()
    thread.start()
    thread.join(timeout=0.5)

    assert not thread.is_alive()
    assert time.monotonic() - started < 0.5
    assert result[0].state == "closed"
    assert not workspace.exists()


def test_serve_failure_still_allows_nonblocking_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = CompanionHTTPServer(tmp_path, JOB_ID)

    def fail_serve(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("serve failed")

    monkeypatch.setattr("http.server.ThreadingHTTPServer.serve_forever", fail_serve)
    serve = threading.Thread(target=server.serve_forever, daemon=True)
    serve.start()
    serve.join(timeout=0.5)
    closer = threading.Thread(target=server.close, daemon=True)
    closer.start()
    closer.join(timeout=0.5)

    assert not serve.is_alive()
    assert not closer.is_alive()
    assert server.serve_failed is True
    assert server.session.snapshot().state == "closed"


def test_close_retries_workspace_cleanup_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = CompanionHTTPServer(tmp_path, JOB_ID)
    workspace = server.session.workspace.path
    original = JobWorkspace.cleanup
    attempts = 0

    def flaky_cleanup(job_workspace: JobWorkspace) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("locked")
        original(job_workspace)

    monkeypatch.setattr(JobWorkspace, "cleanup", flaky_cleanup)

    first = server.close()
    assert first.state == "cleanup_required"
    assert workspace.exists()
    second = server.close()
    assert second.state == "closed"
    assert second.cleaned is True
    assert not workspace.exists()


def test_cancel_reports_cleanup_required_and_retry_truthfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with running_server(tmp_path) as server:
        cookie, csrf = claim(server)
        headers = {"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": server.origin}
        original = JobWorkspace.cleanup
        attempts = 0

        def flaky_cleanup(workspace: JobWorkspace) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("locked")
            original(workspace)

        monkeypatch.setattr(JobWorkspace, "cleanup", flaky_cleanup)
        first = request(server, "POST", "/api/cancel", body=b"", headers=headers)
        assert first.status == 202
        assert read_json(first) == {"status": "cleanup_required", "cleaned": False}
        status = request(server, "GET", "/api/session", headers={"Cookie": cookie})
        assert read_json(status)["state"] == "cleanup_required"

        second = request(server, "POST", "/api/cancel", body=b"", headers=headers)
        assert second.status == 200
        assert read_json(second) == {"status": "cancelled", "cleaned": True}
