"""artoke_sync — 로컬 HTTP 서버 픽스처로 네트워크 없이 검증한다."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from maxmcp.helpers.artoke_sync import fetch_manifest, sync_motions

BVH_BODY = b"HIERARCHY\nROOT Hips\n"
ETAG = '"abc123"'
PHASE_BODY = json.dumps(
    {
        "version": 1,
        "clips": {
            "walk.bvh": {
                "leftContacts": [0, 30],
                "rightContacts": [15],
                "cycleFrames": 30,
                "fps": 30,
                "metresPerSecond": 1.3,
                "rigHeight": 100.0,
            }
        },
    }
).encode()


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
    # phase.json 은 매니페스트 항목이 아니라 별도로 받는다. 404 로 두는 케이스가
    # 있어야 "위상 없이도 동기화 자체는 성립한다"를 검증할 수 있다.
    serve_phase = True

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
        elif self.path == "/motions/phase.json":
            if self.serve_phase:
                self.send_response(200)
                self.send_header("Content-Length", str(len(PHASE_BODY)))
                self.end_headers()
                self.wfile.write(PHASE_BODY)
            else:
                self.send_response(404)
                self.end_headers()
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


def test_sync_downloads_phase_json(tmp_path, server) -> None:
    """블렌드는 발접지 위상 없이 돌지 못하고, phase.json 은 매니페스트 항목이 아니다."""
    out = sync_motions(str(tmp_path), base=server)
    assert out["phase_warning"] is None
    phase = json.loads((tmp_path / "phase.json").read_text(encoding="utf-8"))
    assert phase["clips"]["walk.bvh"]["metresPerSecond"] == 1.3
    assert phase["clips"]["walk.bvh"]["rigHeight"] == 100.0


def test_sync_downloads_phase_json_even_when_motions_are_unchanged(tmp_path, server) -> None:
    """모션이 그대로여도(ETag 304) 위상 파일은 로컬에 없을 수 있다."""
    first = sync_motions(str(tmp_path), base=server)
    (tmp_path / "phase.json").unlink()
    again = sync_motions(str(tmp_path), base=server, etag=first["etag"])
    assert again["unchanged"] is True
    assert (tmp_path / "phase.json").exists()


def test_sync_survives_a_missing_phase_json(tmp_path, server) -> None:
    """위상 파일이 없어도 모션 동기화는 성립한다 — 경고만 달고 블렌드만 못 쓴다."""
    _Handler.serve_phase = False
    try:
        out = sync_motions(str(tmp_path), base=server)
    finally:
        _Handler.serve_phase = True
    assert sorted(out["downloaded"]) == ["artoke_run.bvh", "artoke_walk.bvh"]
    assert "phase.json" in out["phase_warning"]
    assert not (tmp_path / "phase.json").exists()
