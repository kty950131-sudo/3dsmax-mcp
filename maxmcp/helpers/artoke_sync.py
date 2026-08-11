"""artoke.com 에서 BVH 모션을 동기화한다.

github_sync 와 달리 인증이 필요 없다 — 판매 배포에서 구매자가 쓸 경로다.
모션은 Vercel 의 public/ 정적 서빙으로 내려받고, 목록은 같은 곳의
``manifest.json`` (scripts/gen-motions-manifest.mjs 가 생성) 을 읽는다.
정적 파일에는 ETag 가 자동으로 붙으므로, 폴링은 If-None-Match 304 로
트래픽 없이 돈다.

어떤 파일을 받을지는 github_sync.plan_sync 를 그대로 재사용한다 —
크기 비교 동기화 계획은 전송 수단과 무관하다.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from maxmcp.helpers.github_sync import DEFAULT_PREFIX, plan_sync

DEFAULT_BASE = "https://artoke.com/motions"
_TIMEOUT = 30


def fetch_manifest(
    base: str = DEFAULT_BASE, etag: Optional[str] = None
) -> tuple[Optional[list[dict]], Optional[str]]:
    """모션 목록과 새 ETag 를 돌려준다. 304(변경 없음)면 목록은 None 이다."""
    request = urllib.request.Request(f"{base}/manifest.json")
    if etag:
        request.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
            new_etag = response.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, etag
        raise RuntimeError(f"manifest 요청 실패 (HTTP {exc.code}): {base}/manifest.json") from exc
    motions = data.get("motions")
    if not isinstance(motions, list):
        raise RuntimeError("manifest.json 형식이 다릅니다 — 'motions' 배열이 없음")
    return motions, new_etag


def download_motion(name: str, dest_file: Path, base: str = DEFAULT_BASE) -> None:
    with urllib.request.urlopen(f"{base}/{name}", timeout=_TIMEOUT) as response:
        Path(dest_file).write_bytes(response.read())


def sync_motions(
    dest_dir: str,
    base: str = DEFAULT_BASE,
    prefix: str = DEFAULT_PREFIX,
    etag: Optional[str] = None,
) -> dict:
    """새/변경된 모션을 dest_dir 에 <prefix><name> 으로 받는다.

    반환: {"downloaded": [로컬 이름], "remote_total": int, "etag": str|None,
           "unchanged": bool}  — unchanged 는 ETag 304 로 아무것도 안 봤다는 뜻.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    remote, new_etag = fetch_manifest(base, etag)
    if remote is None:
        return {"downloaded": [], "remote_total": -1, "etag": new_etag, "unchanged": True}
    local_sizes = {p.name: p.stat().st_size for p in dest.glob("*.bvh")}
    todo = plan_sync(remote, local_sizes, prefix)
    for entry in todo:
        download_motion(entry["name"], dest / (prefix + entry["name"]), base=base)
    return {
        "downloaded": [prefix + e["name"] for e in todo],
        "remote_total": len(remote),
        "etag": new_etag,
        "unchanged": False,
    }
