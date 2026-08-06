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
