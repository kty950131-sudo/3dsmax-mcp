"""3ds Max 안에서 BVH Studio 창을 띄운다.

창은 **세션당 한 번만 만든다.** QWebEngineView 를 반복 생성/파괴하면 Max 가
죽는다 (Task 9 실측 — 세 번째 재생성에서 프로세스 종료). 다시 실행하면 기존
창을 앞으로 올리고 페이지만 새로 읽는다.
"""

import os
from typing import Any

from src.ui.studio import _session

PAGE = "studio_draft.html"


def _cache_dir() -> str:
    try:
        from pymxs import runtime as rt

        return os.path.join(str(rt.getDir(rt.Name("userScripts"))), "bvh_studio_cache")
    except Exception:
        import tempfile

        return os.path.join(tempfile.gettempdir(), "bvh_studio_cache")


def launch() -> Any:
    if _session.window is not None:
        _session.window.show()
        _session.window.raise_()
        _session.window.load_page(PAGE)  # 코드가 바뀌었을 수 있으니 페이지는 새로 읽는다
        return _session.window

    from src.ui.studio.bridge import StudioBridge
    from src.ui.studio.compat import max_main_window
    from src.ui.studio.webhost import WebHost

    bridge = StudioBridge(_cache_dir())
    host = WebHost(bridge, title="BVH Studio", parent=max_main_window())
    host.show()
    host.raise_()
    host.load_page(PAGE)
    _session.window = host
    return host


if __name__ == "__main__":
    launch()
