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

# 블렌드가 찾는 이름과 같아야 한다 — 여기서 받는 파일을 저쪽이 읽는다.
from maxmcp.helpers.blend import PHASE_NAME

DEFAULT_BASE = "https://artoke.com/motions"
_TIMEOUT = 30


def fetch_manifest(
    base: str = DEFAULT_BASE, etag: Optional[str] = None
) -> tuple[Optional[dict], Optional[str]]:
    """매니페스트 전체와 새 ETag 를 돌려준다. 304(변경 없음)면 매니페스트는 None 이다.

    목록('motions')만 잘라 주지 않는 이유: 매니페스트에는 사이트의 분류
    ('categories')도 실려 오고, 라이브러리가 그걸로 클립을 사이트와 같은
    선반으로 그룹핑한다.
    """
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
    if not isinstance(data.get("motions"), list):
        raise RuntimeError("manifest.json 형식이 다릅니다 — 'motions' 배열이 없음")
    return data, new_etag


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

    # phase.json 은 모션이 아니라 매니페스트 항목에 없다. 그런데 블렌드가 발접지 위상
    # 없이는 돌지 못하니 같은 base 에서 따로 받는다. ETag 304 로 조기 반환하는 경로에서도
    # 빠지지 않도록 매니페스트보다 먼저 받는다 — 모션이 그대로여도 위상 파일이 로컬에
    # 없을 수 있다. 실패는 치명적이지 않다: 블렌드만 못 쓰고 나머지 동기화는 성립한다.
    phase_warning: Optional[str] = None
    try:
        download_motion(PHASE_NAME, dest / PHASE_NAME, base=base)
    except Exception as error:  # noqa: BLE001 - 네트워크 실패로 동기화 전체를 깨지 않는다
        phase_warning = f"{PHASE_NAME} 를 받지 못했습니다: {error}"

    manifest, new_etag = fetch_manifest(base, etag)
    if manifest is None:
        return {
            "downloaded": [],
            "remote_total": -1,
            "etag": new_etag,
            "unchanged": True,
            "phase_warning": phase_warning,
        }
    remote = manifest["motions"]
    local_sizes = {p.name: p.stat().st_size for p in dest.glob("*.bvh")}
    todo = plan_sync(remote, local_sizes, prefix)
    for entry in todo:
        download_motion(entry["name"], dest / (prefix + entry["name"]), base=base)
    # 매니페스트를 폴더에 남긴다 — 라이브러리가 오프라인에서도 분류를 알 수 있게.
    (dest / "artoke-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "downloaded": [prefix + e["name"] for e in todo],
        "remote_total": len(remote),
        "etag": new_etag,
        "unchanged": False,
        "phase_warning": phase_warning,
    }
