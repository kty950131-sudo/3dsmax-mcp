"""Loopback-only HTTP companion for selecting one local source video."""

from __future__ import annotations

from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import base64
import binascii
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from .session import (
    CompanionSession,
    SessionRejected,
    SessionSnapshot,
    UploadRejected,
)


_COOKIE_NAME = "artoke_local_ui"
_MAX_ENCODED_DISPLAY_NAME_BYTES = 960
_MAX_DECODED_DISPLAY_NAME_BYTES = 720
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'none'; "
    "connect-src 'self'; object-src 'none'; frame-src 'none'; base-uri 'none'; "
    "form-action 'self'"
)
_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def _cookie_value(raw: str | None) -> str | None:
    if not raw:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except CookieError:
        return None
    morsel = cookie.get(_COOKIE_NAME)
    return morsel.value if morsel is not None else None


def _decode_display_name(encoded: str | None) -> str:
    if (
        not isinstance(encoded, str)
        or not 1 <= len(encoded) <= _MAX_ENCODED_DISPLAY_NAME_BYTES
        or _BASE64URL.fullmatch(encoded) is None
    ):
        raise ValueError
    padding = "=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        decoded = raw.decode("utf-8", errors="strict")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ValueError from None
    canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if (
        not 1 <= len(raw) <= _MAX_DECODED_DISPLAY_NAME_BYTES
        or not decoded
        or canonical != encoded
    ):
        raise ValueError
    return decoded


class CompanionHTTPServer(ThreadingHTTPServer):
    """A one-session local server bound to an OS-selected IPv4 loopback port."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        workspace_root: Path,
        job_id: str,
        *,
        max_source_bytes: int = 2_147_483_648,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        self.session = CompanionSession.create(
            workspace_root,
            job_id,
            max_source_bytes=max_source_bytes,
            chunk_size=chunk_size,
        )
        self._close_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._serve_started = threading.Event()
        self._serve_stopped = threading.Event()
        self._serve_running = False
        self._serve_failed = False
        self._closed = False
        try:
            super().__init__(("127.0.0.1", 0), _CompanionRequestHandler)
        except BaseException:
            self.session.close()
            raise

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def authority(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def origin(self) -> str:
        return f"http://{self.authority}"

    @property
    def serve_failed(self) -> bool:
        with self._lifecycle_lock:
            return self._serve_failed

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        with self._lifecycle_lock:
            if self._closed:
                self._serve_stopped.set()
                return
            self._serve_running = True
            self._serve_started.set()
        try:
            super().serve_forever(poll_interval=poll_interval)
        except Exception:
            with self._lifecycle_lock:
                self._serve_failed = True
        finally:
            with self._lifecycle_lock:
                self._serve_running = False
                self._serve_stopped.set()

    def close(self) -> SessionSnapshot:
        with self._close_lock:
            if self._closed:
                return self.session.close()
            self._closed = True
            with self._lifecycle_lock:
                running = self._serve_running
            if running:
                self.shutdown()
            self.server_close()
            return self.session.close()


class _CompanionRequestHandler(BaseHTTPRequestHandler):
    server: CompanionHTTPServer
    protocol_version = "HTTP/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if not self._valid_authority():
            self._json(HTTPStatus.FORBIDDEN, "invalid_authority")
            return
        if not self._valid_optional_origin():
            self._json(HTTPStatus.FORBIDDEN, "invalid_origin")
            return
        if path == "/":
            self._serve_shell()
            return
        if path in {"/app.js", "/styles.css"}:
            if not self._authorize_browser():
                return
            self._serve_asset(path)
            return
        if path == "/api/session":
            if not self._authorize_browser():
                return
            snapshot = self.server.session.snapshot()
            self._send_json(
                HTTPStatus.OK,
                {
                    "csrfToken": self.server.session.csrf_token,
                    "state": snapshot.state,
                    "sizeBytes": snapshot.size_bytes,
                    "cleaned": snapshot.cleaned,
                },
            )
            return
        self._json(HTTPStatus.NOT_FOUND, "not_found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if not self._valid_authority():
            self._json(HTTPStatus.FORBIDDEN, "invalid_authority")
            return
        if self.headers.get("Origin") != self.server.origin:
            self._json(HTTPStatus.FORBIDDEN, "invalid_origin")
            return
        if not self._authorize_mutation():
            return
        if path == "/api/source":
            self._receive_source()
            return
        if path == "/api/cancel":
            snapshot = self.server.session.cancel()
            status = (
                HTTPStatus.ACCEPTED
                if snapshot.state in {"cancelling", "cleanup_required"}
                else HTTPStatus.OK
            )
            self._send_json(
                status,
                {"status": snapshot.state, "cleaned": snapshot.cleaned},
            )
            return
        self._json(HTTPStatus.NOT_FOUND, "not_found")

    def do_HEAD(self) -> None:
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")

    do_PUT = do_HEAD
    do_PATCH = do_HEAD
    do_DELETE = do_HEAD
    do_OPTIONS = do_HEAD

    def _serve_shell(self) -> None:
        cookie = _cookie_value(self.headers.get("Cookie"))
        try:
            issue_cookie = self.server.session.claim_browser(cookie)
        except SessionRejected as exc:
            self._json(HTTPStatus.CONFLICT, exc.code)
            return
        extra = None
        if issue_cookie:
            extra = {
                "Set-Cookie": (
                    f"{_COOKIE_NAME}={self.server.session.browser_cookie}; "
                    "HttpOnly; SameSite=Strict; Path=/"
                )
            }
        self._serve_asset("/", extra_headers=extra)

    def _serve_asset(
        self,
        path: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        name, content_type = _ASSETS[path]
        resource = files("maxmcp.local_ingest").joinpath("web", name)
        try:
            data = resource.read_bytes()
        except OSError:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, "asset_unavailable")
            return
        self._send(HTTPStatus.OK, data, content_type, extra_headers=extra_headers)

    def _receive_source(self) -> None:
        if self.headers.get("Content-Type") != "application/octet-stream":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "raw_source_required")
            return
        lengths = self.headers.get_all("Content-Length", failobj=[])
        transfer_encoding = self.headers.get("Transfer-Encoding")
        if transfer_encoding is not None or len(lengths) != 1:
            self._json(HTTPStatus.LENGTH_REQUIRED, "content_length_required")
            return
        raw_length = lengths[0]
        if re.fullmatch(r"[1-9][0-9]*", raw_length or "") is None:
            code = "empty_source" if raw_length == "0" else "invalid_content_length"
            self._json(HTTPStatus.BAD_REQUEST, code)
            return
        content_length = int(raw_length)
        try:
            display_name = _decode_display_name(
                self.headers.get("X-Artoke-Filename")
            )
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, "invalid_display_name")
            return
        try:
            self.server.session.receive_source(
                self.rfile,
                content_length,
                display_name,
            )
        except UploadRejected as exc:
            statuses = {
                "source_too_large": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "source_already_selected": HTTPStatus.CONFLICT,
                "cancelled": HTTPStatus.CONFLICT,
                "unsafe_workspace": HTTPStatus.CONFLICT,
                "empty_source": HTTPStatus.BAD_REQUEST,
                "invalid_content_length": HTTPStatus.BAD_REQUEST,
                "incomplete_upload": HTTPStatus.BAD_REQUEST,
                "invalid_source_stream": HTTPStatus.BAD_REQUEST,
                "source_write_failed": HTTPStatus.INTERNAL_SERVER_ERROR,
            }
            self._json(statuses.get(exc.code, HTTPStatus.BAD_REQUEST), exc.code)
            return
        self._send_json(
            HTTPStatus.CREATED,
            {"status": "source_received", "sizeBytes": content_length},
        )

    def _authorize_browser(self) -> bool:
        try:
            self.server.session.authorize_browser(
                _cookie_value(self.headers.get("Cookie"))
            )
            return True
        except SessionRejected as exc:
            self._json(HTTPStatus.FORBIDDEN, exc.code)
            return False

    def _authorize_mutation(self) -> bool:
        try:
            self.server.session.authorize_mutation(
                _cookie_value(self.headers.get("Cookie")),
                self.headers.get("X-CSRF-Token"),
            )
            return True
        except SessionRejected as exc:
            self._json(HTTPStatus.FORBIDDEN, exc.code)
            return False

    def _valid_authority(self) -> bool:
        values = self.headers.get_all("Host", failobj=[])
        return len(values) == 1 and values[0] == self.server.authority

    def _valid_optional_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin == self.server.origin

    def _json(self, status: HTTPStatus, code: str) -> None:
        self._send_json(status, {"error": code})

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        self._send(
            status,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return


__all__ = ["CompanionHTTPServer"]
