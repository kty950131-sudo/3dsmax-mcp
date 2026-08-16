"""artoke_sync — 로컬 HTTP 서버 픽스처로 네트워크 없이 검증한다."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.helpers.artoke_sync import fetch_manifest, sync_motions

BVH_BODY = b"HIERARCHY\nROOT Hips\n"
ETAG = '"abc123"'


MANIFEST = {
    "version": 1,
    "categories": [
        {
            "slug": "locomotion",
            "label": "이동",
            "subs": [{"slug": "walk", "label": "걷기"}, {"slug": "run", "label": "달리기"}],
        }
    ],
    "motions": [
        {"name": "walk.bvh", "size": len(BVH_BODY), "category": "locomotion", "sub": "walk"},
        {"name": "run.bvh", "size": len(BVH_BODY), "category": "locomotion", "sub": "run"},
    ],
}


class _Handler(BaseHTTPRequestHandler):
    manifest = MANIFEST

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


def test_fetch_manifest_returns_manifest_and_etag(server) -> None:
    manifest, etag = fetch_manifest(base=server)
    assert [m["name"] for m in manifest["motions"]] == ["walk.bvh", "run.bvh"]
    # 분류는 BVH Studio 가 사이트와 같은 선반으로 그룹핑하는 근거다 — 통째로 전달
    assert manifest["categories"][0]["slug"] == "locomotion"
    assert etag == ETAG


def test_fetch_manifest_304_returns_none(server) -> None:
    manifest, etag = fetch_manifest(base=server, etag=ETAG)
    assert manifest is None
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


def test_sync_writes_manifest_sidecar(tmp_path, server) -> None:
    """동기화가 매니페스트를 폴더에 남겨, 라이브러리가 오프라인에서도 분류를 안다."""
    sync_motions(str(tmp_path), base=server)
    sidecar = json.loads((tmp_path / "artoke-manifest.json").read_text(encoding="utf-8"))
    assert sidecar["categories"][0]["label"] == "이동"
    assert sidecar["motions"][0]["category"] == "locomotion"


def test_fetch_manifest_bad_shape_raises(tmp_path, server) -> None:
    _Handler.manifest = {"nope": True}
    try:
        with pytest.raises(RuntimeError, match="motions"):
            fetch_manifest(base=server)
    finally:
        _Handler.manifest = MANIFEST
