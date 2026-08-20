"""JS 에 노출되는 슬롯 모음. 반환은 항상 JSON 문자열이다.

파이썬 예외가 채널 너머로 새면 JS 쪽 콜백이 조용히 안 불리고 페이지가 멈춘 것처럼
보인다. 원인을 화면에서 알 수 없으므로, 모든 슬롯이 예외를 잡아
``{"ok": false, "error": ...}`` 로 돌려준다.
"""

import json
import os
import traceback
from typing import Any, Callable, Optional

from maxmcp.ui.studio.compat import QtCore, QtWidgets
from maxmcp.ui.studio.library import cache_path, delete_clip, load_shelf, scan
from maxmcp.ui.studio import settings
from maxmcp.ui.studio.thumb import load_pose_data
from maxmcp.ui.studio.video_jobs import VideoJobController


def reply(fn: Callable[[], Any]) -> str:
    """슬롯 본문을 감싸 성공/실패를 같은 모양의 JSON 으로 만든다."""
    try:
        return json.dumps({"ok": True, "data": fn()})
    except Exception as exc:  # 채널 너머로 예외를 새게 두지 않는다
        return json.dumps(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(limit=3),
            }
        )


class StudioBridge(QtCore.QObject):
    """``bridge`` 라는 이름으로 JS 전역에 붙는다 (webhost 가 등록한다)."""

    def __init__(self, cache_dir: str, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self._cache_dir = cache_dir
        self._video_jobs = VideoJobController()

    @QtCore.Slot(str, result=str)
    def ping(self, text: str) -> str:
        """왕복 확인용. 채널이 살아 있는지만 본다."""
        return reply(lambda: {"echo": text, "qt": QtCore.qVersion()})

    @QtCore.Slot(result=str)
    def read_settings(self) -> str:
        """창을 닫아도 남아야 하는 UI 상태 전부. 부팅 때 한 번 읽는다."""
        return reply(lambda: settings.load(self._cache_dir))

    @QtCore.Slot(str, result=str)
    def write_setting(self, payload_json: str) -> str:
        """키 하나를 저장한다. ``{"key": ..., "value": ...}``."""

        def run() -> dict:
            payload = json.loads(payload_json)
            key = payload.get("key")
            if not isinstance(key, str) or not key:
                raise ValueError("key 가 필요합니다")
            return settings.save(self._cache_dir, key, payload.get("value"))

        return reply(run)

    @QtCore.Slot(str, result=str)
    def pick_folder(self, start: str) -> str:
        """폴더 선택 창을 띄우고 고른 경로를 돌려준다. 취소하면 빈 문자열.

        경로를 손으로 치게 두면 오타 하나에 "이 폴더에 클립이 없습니다" 만 뜨고,
        무엇이 틀렸는지는 화면 어디에도 없다.
        """

        def run() -> dict:
            from maxmcp.ui.studio.compat import QtWidgets

            picked = QtWidgets.QFileDialog.getExistingDirectory(
                None, "클립 폴더 고르기", start or ""
            )
            return {"folder": picked or ""}

        return reply(run)

    @QtCore.Slot(str, result=str)
    def list_clips(self, folder: str) -> str:
        """클립 목록 + 사이트의 분류. 그리드가 카테고리→역할→세부로 그룹핑한다."""
        return reply(
            lambda: {
                "clips": [
                    {
                        "stem": clip.stem,
                        "path": clip.path,
                        "tags": list(clip.tags),
                        "category": clip.category,
                        "sub": clip.sub,
                        "detail": clip.detail,
                        "local": clip.local,
                        "polished": clip.polished,
                    }
                    for clip in scan(folder)
                ],
                "categories": load_shelf(folder)["categories"],
                # 폴더가 없는 것과 폴더에 .bvh 가 없는 것은 사용자가 할 일이
                # 다르다 — 앞은 경로를 고쳐야 하고 뒤는 파일을 넣어야 한다.
                # scan 은 둘 다 빈 목록이라 여기서 갈라 준다.
                "exists": os.path.isdir(folder),
            }
        )

    @QtCore.Slot(str, result=str)
    def pose_data(self, clip_path: str) -> str:
        """포즈 좌표와 뼈대. 첫 호출만 느리고 이후는 캐시다."""
        return reply(lambda: load_pose_data(clip_path, self._cache_dir))

    @QtCore.Slot(str, result=str)
    def blend_tiers(self, folder: str) -> str:
        """이 폴더에서 블렌드에 쓸 수 있는 속도층 목록. 없으면 빈 목록.

        위상 파일이 있는지도 같이 알려준다 — 없으면 발접지 정렬 없이 섞게 되고 발이
        엇갈린 클립이 나오므로, UI 가 그 이유를 말하며 잠글 수 있어야 한다.
        """

        def run() -> dict:
            from maxmcp.helpers.blend import MANIFEST_NAME, PHASE_NAME, discover_tiers

            manifest_path = os.path.join(folder, MANIFEST_NAME)
            if not os.path.exists(manifest_path):
                return {"tiers": [], "hasPhase": False}
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            entries = manifest["motions"] if isinstance(manifest, dict) else manifest
            return {
                "tiers": discover_tiers(entries),
                "hasPhase": os.path.exists(os.path.join(folder, PHASE_NAME)),
            }

        return reply(run)

    @QtCore.Slot(str, result=str)
    def bake_blend(self, payload_json: str) -> str:
        """8방향 세트를 임의 각도로 굳혀 임시 BVH 로 쓰고 경로를 돌려준다.

        임포트는 하지 않는다. `import_clip` / `retarget_clip` 이 이미 파일 경로를
        받으므로(`p["path"]`), 여기서 파일 하나만 만들어 주면 바이패드 생성·트림·
        미러·팔 간격·배치 간격이 전부 그대로 따라온다. pymxs 를 쓰지 않으므로 Max
        밖에서도 도는 슬롯 구역에 있다.
        """

        def run() -> dict:
            from maxmcp.helpers.blend import bake_blend_file

            p = json.loads(payload_json)
            return bake_blend_file(
                p["folder"], float(p["angle"]), float(p.get("speed_t", 0.0))
            )

        return reply(run)

    @QtCore.Slot(str, result=str)
    def delete_clip(self, payload_json: str) -> str:
        """로컬 라이브러리에서 클립 삭제. 사이트에는 아무 요청도 보내지 않는다."""

        def run() -> dict:
            p = json.loads(payload_json)
            out = delete_clip(p["folder"], p["path"])
            # 포즈 캐시도 지운다 — 남겨두면 같은 경로의 새 클립이 옛 포즈를 쓴다
            try:
                os.remove(cache_path(p["path"], self._cache_dir))
            except OSError:
                pass
            return out

        return reply(run)

    @QtCore.Slot(str, result=str)
    def open_external(self, url: str) -> str:
        """기본 브라우저로 URL을 연다.

        QWebEngine 안에서 window.open 은 새 창을 우리 웹뷰에 띄우려 들거나
        조용히 무시된다 — 외부 브라우저가 목적이면 QDesktopServices 가 정도다.
        https 만 받는다: 슬롯은 JS 쪽 어떤 문자열로도 불릴 수 있는 경계다.
        """

        def run() -> dict:
            if not url.startswith("https://"):
                raise ValueError(f"https URL만 엽니다: {url}")
            from maxmcp.ui.studio.compat import QtCore as _qtcore, QtGui

            QtGui.QDesktopServices.openUrl(_qtcore.QUrl(url))
            return {"opened": url}

        return reply(run)

    @QtCore.Slot(result=str)
    def choose_video(self) -> str:
        def run() -> dict:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                None, "영상 추가", "", "Video (*.mp4 *.mov *.mkv *.avi *.webm)"
            )
            return {"cancelled": not bool(path), "path": path}

        return reply(run)

    @QtCore.Slot(str, result=str)
    def start_video_job(self, payload_json: str) -> str:
        return reply(lambda: self._video_jobs.start(json.loads(payload_json)))

    @QtCore.Slot(str, result=str)
    def video_job_status(self, job_id: str) -> str:
        return reply(lambda: self._video_jobs.status(job_id))

    @QtCore.Slot(str, result=str)
    def cancel_video_job(self, job_id: str) -> str:
        return reply(lambda: self._video_jobs.cancel(job_id))

    # ---- 아래부터는 Max 안에서만 동작한다 (pymxs 필요) ----

    @QtCore.Slot(str, result=str)
    def import_clip(self, payload_json: str) -> str:
        """바이패드 생성 + 클립 임포트. payload 는 JS 쪽 상태를 dict 하나로 받는다."""

        def run() -> dict:
            from maxmcp.ui.studio.maxbridge import import_clip

            p = json.loads(payload_json)
            msg = import_clip(
                p["path"],
                p.get("name", ""),
                bool(p.get("convert", True)),
                float(p.get("x_offset", 0.0)),
                speed=float(p.get("speed", 1.0)),
                trim=(float(p.get("trim_start", 0.0)), float(p.get("trim_end", 1.0))),
                time_map=p.get("time_map"),
                mirror=bool(p.get("mirror", False)),
                arm_points=[(int(f), float(d)) for f, d in p.get("arm_points", [])],
            )
            if msg.startswith("ERROR"):
                raise RuntimeError(msg)
            return {"message": msg}

        return reply(run)

    @QtCore.Slot(str, result=str)
    def retarget_clip(self, payload_json: str) -> str:
        """기존 바이패드에 클립 로드 — 새 바이패드를 만들지 않는다."""

        def run() -> dict:
            from maxmcp.ui.studio.maxbridge import retarget_clip

            p = json.loads(payload_json)
            msg = retarget_clip(
                p["path"],
                p["biped"],
                bool(p.get("convert", True)),
                speed=float(p.get("speed", 1.0)),
                trim=(float(p.get("trim_start", 0.0)), float(p.get("trim_end", 1.0))),
                time_map=p.get("time_map"),
                mirror=bool(p.get("mirror", False)),
                arm_points=[(int(f), float(d)) for f, d in p.get("arm_points", [])],
            )
            if msg.startswith("ERROR"):
                raise RuntimeError(msg)
            return {"message": msg}

        return reply(run)

    @QtCore.Slot(result=str)
    def scene_bipeds(self) -> str:
        def run() -> list[str]:
            from maxmcp.ui.studio.maxbridge import scene_bipeds

            return scene_bipeds()

        return reply(run)

    @QtCore.Slot(str, result=str)
    def choose_bvh_path(self, suggested: str) -> str:
        """내보낼 BVH 저장 위치를 묻는다. 취소하면 빈 문자열.

        경로를 손으로 치게 두면 오타 하나에 조용히 엉뚱한 데 쓰이고, 덮어쓰기
        확인도 사라진다. `pick_folder` 와 같은 이유로 대화상자를 쓴다.
        """

        def run() -> dict:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                None, "BVH 로 내보내기", suggested or "", "BVH (*.bvh)"
            )
            return {"cancelled": not bool(path), "path": path}

        return reply(run)

    @QtCore.Slot(str, result=str)
    def export_biped_bvh(self, payload_json: str) -> str:
        def run() -> dict:
            from maxmcp.ui.studio.biped_export import export_biped_bvh

            p = json.loads(payload_json)
            msg = export_biped_bvh(p["biped"], p["path"])
            if msg.startswith("ERROR"):
                raise RuntimeError(msg)
            return {"message": msg}

        return reply(run)

    @QtCore.Slot(str, result=str)
    def set_in_place(self, payload_json: str) -> str:
        def run() -> dict:
            from maxmcp.ui.studio.maxbridge import set_in_place

            p = json.loads(payload_json)
            msg = set_in_place(p["biped"], bool(p.get("on", True)))
            if msg.startswith("ERROR"):
                raise RuntimeError(msg)
            return {"message": msg}

        return reply(run)

    @QtCore.Slot(str, result=str)
    def set_arm_space_visible(self, payload_json: str) -> str:
        def run() -> dict:
            from maxmcp.ui.studio.maxbridge import set_arm_space_visible

            p = json.loads(payload_json)
            msg = set_arm_space_visible(p["biped"], bool(p.get("visible", False)))
            if msg.startswith("ERROR"):
                raise RuntimeError(msg)
            return {"message": msg}

        return reply(run)

    @QtCore.Slot(str, result=str)
    def apply_arm_space(self, payload_json: str) -> str:
        def run() -> dict:
            from maxmcp.ui.studio.maxbridge import apply_arm_space

            p = json.loads(payload_json)
            msg = apply_arm_space(
                p["biped"], [(int(f), float(d)) for f, d in p.get("points", [])]
            )
            if msg.startswith("ERROR"):
                raise RuntimeError(msg)
            return {"message": msg}

        return reply(run)

    @QtCore.Slot(str, result=str)
    def send_to_mixer(self, payload_json: str) -> str:
        def run() -> dict:
            from maxmcp.ui.studio.maxbridge import send_to_mixer

            p = json.loads(payload_json)
            msg = send_to_mixer(p["biped"], p["clips_dir"])
            if msg.startswith("ERROR"):
                raise RuntimeError(msg)
            return {"message": msg}

        return reply(run)

    @QtCore.Slot(str, result=str)
    def sync_from_artoke(self, folder: str) -> str:
        """artoke.com 공개 manifest 로 모션 동기화. 인증 불필요 — 판매 배포 경로."""

        def run() -> dict:
            from maxmcp.helpers.artoke_sync import sync_motions

            return sync_motions(folder)

        return reply(run)

    @QtCore.Slot(str, result=str)
    def sync_from_github(self, folder: str) -> str:
        """gh CLI 경로 (개발자 전용 폴백 — gh auth 가 있는 PC 에서만 동작)."""

        def run() -> dict:
            from maxmcp.helpers.github_sync import sync_motions

            return sync_motions(folder)

        return reply(run)
