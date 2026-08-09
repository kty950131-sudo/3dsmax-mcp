"""JS 에 노출되는 슬롯 모음. 반환은 항상 JSON 문자열이다.

파이썬 예외가 채널 너머로 새면 JS 쪽 콜백이 조용히 안 불리고 페이지가 멈춘 것처럼
보인다. 원인을 화면에서 알 수 없으므로, 모든 슬롯이 예외를 잡아
``{"ok": false, "error": ...}`` 로 돌려준다.
"""

import json
import traceback
from typing import Any, Callable, Optional

from src.ui.studio.compat import QtCore
from src.ui.studio.library import scan
from src.ui.studio.thumb import load_pose_data


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

    @QtCore.Slot(str, result=str)
    def ping(self, text: str) -> str:
        """왕복 확인용. 채널이 살아 있는지만 본다."""
        return reply(lambda: {"echo": text, "qt": QtCore.qVersion()})

    @QtCore.Slot(str, result=str)
    def list_clips(self, folder: str) -> str:
        return reply(
            lambda: [
                {"stem": clip.stem, "path": clip.path, "tags": list(clip.tags)}
                for clip in scan(folder)
            ]
        )

    @QtCore.Slot(str, result=str)
    def pose_data(self, clip_path: str) -> str:
        """포즈 좌표와 뼈대. 첫 호출만 느리고 이후는 캐시다."""
        return reply(lambda: load_pose_data(clip_path, self._cache_dir))

    # ---- 아래부터는 Max 안에서만 동작한다 (pymxs 필요) ----

    @QtCore.Slot(str, result=str)
    def import_clip(self, payload_json: str) -> str:
        """바이패드 생성 + 클립 임포트. payload 는 JS 쪽 상태를 dict 하나로 받는다."""

        def run() -> dict:
            from src.ui.studio.maxbridge import import_clip

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
            )
            if msg.startswith("ERROR"):
                raise RuntimeError(msg)
            return {"message": msg}

        return reply(run)

    @QtCore.Slot(result=str)
    def scene_bipeds(self) -> str:
        def run() -> list[str]:
            from src.ui.studio.maxbridge import scene_bipeds

            return scene_bipeds()

        return reply(run)

    @QtCore.Slot(str, result=str)
    def apply_arm_space(self, payload_json: str) -> str:
        def run() -> dict:
            from src.ui.studio.maxbridge import apply_arm_space

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
            from src.ui.studio.maxbridge import send_to_mixer

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
            from src.helpers.artoke_sync import sync_motions

            return sync_motions(folder)

        return reply(run)

    @QtCore.Slot(str, result=str)
    def sync_from_github(self, folder: str) -> str:
        """gh CLI 경로 (개발자 전용 폴백 — gh auth 가 있는 PC 에서만 동작)."""

        def run() -> dict:
            from src.helpers.github_sync import sync_motions

            return sync_motions(folder)

        return reply(run)
