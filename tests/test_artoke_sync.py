"""artoke_sync — 로컬 HTTP 서버 픽스처로 네트워크 없이 검증한다."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from maxmcp.helpers.artoke_sync import fetch_manifest, sync_motions

BVH_BODY = b"HIERARCHY\nROOT Hips\n"
ETAG = '"abc123"'


class _Handler(BaseHTTPRequestHandler):
    manifest = {
        "version": 1,
        "motions": [
            {"name": "walk.bvh", "size": len(BVH_BODY)},
            {"name": "run.bvh", "size": len(BVH_BODY)},
        ],
    }

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 관례
        if self.path == "/motions/manifest.json":
            if self.headers.get("If-None-Match") == ETAG:
                self.send_response(304)
                self.end_headers()
                return
            body = json.dumps(self.manifest).encode()
            self.send_response(200)
            self.send_header("ETag", ETAG)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.endswith(".bvh"):
            self.send_response(200)
            self.send_header("Content-Length", str(len(BVH_BODY)))
            self.end_headers()
            self.wfile.write(BVH_BODY)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # 테스트 출력 오염 방지
        pass


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/motions"
    httpd.shutdown()


def test_fetch_manifest_returns_motions_and_etag(server) -> None:
    motions, etag = fetch_manifest(base=server)
    assert [m["name"] for m in motions] == ["walk.bvh", "run.bvh"]
    assert etag == ETAG


def test_fetch_manifest_304_returns_none(server) -> None:
    motions, etag = fetch_manifest(base=server, etag=ETAG)
    assert motions is None
    assert etag == ETAG


def test_sync_downloads_missing_with_prefix(tmp_path, server) -> None:
    out = sync_motions(str(tmp_path), base=server)
    assert sorted(out["downloaded"]) == ["artoke_run.bvh", "artoke_walk.bvh"]
    assert out["remote_total"] == 2
    assert not out["unchanged"]
    assert (tmp_path / "artoke_walk.bvh").read_bytes() == BVH_BODY


def test_sync_skips_up_to_date(tmp_path, server) -> None:
    sync_motions(str(tmp_path), base=server)
    again = sync_motions(str(tmp_path), base=server)
    assert again["downloaded"] == []


def test_sync_with_etag_short_circuits(tmp_path, server) -> None:
    first = sync_motions(str(tmp_path), base=server)
    cached = sync_motions(str(tmp_path), base=server, etag=first["etag"])
    assert cached["unchanged"] is True
    assert cached["downloaded"] == []


def test_fetch_manifest_bad_shape_raises(tmp_path, server) -> None:
    _Handler.manifest = {"nope": True}
    try:
        with pytest.raises(RuntimeError, match="motions"):
            fetch_manifest(base=server)
    finally:
        _Handler.manifest = {
            "version": 1,
            "motions": [
                {"name": "walk.bvh", "size": len(BVH_BODY)},
                {"name": "run.bvh", "size": len(BVH_BODY)},
            ],
        }
