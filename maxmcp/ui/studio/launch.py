"""3ds Max 안에서 BVH Studio 창을 띄운다.

창은 **세션당 한 번만 만든다.** QWebEngineView 를 반복 생성/파괴하면 Max 가
죽는다 (Task 9 실측 — 세 번째 재생성에서 프로세스 종료). 다시 실행하면 기존
창을 앞으로 올리고 페이지만 새로 읽는다.
"""

import importlib
import os
from typing import Any

from maxmcp.ui.studio import _session

PAGE = "studio_draft.html"


def _cache_dir() -> str:
    try:
        from pymxs import runtime as rt

        return os.path.join(str(rt.getDir(rt.Name("userScripts"))), "bvh_studio_cache")
    except Exception:
        import tempfile

        return os.path.join(tempfile.gettempdir(), "bvh_studio_cache")


def _new_bridge() -> Any:
    # bridge 가 딛고 서는 스튜디오/헬퍼 모듈을 의존 순서대로 전부 새로 읽는다.
    # bridge 만 reload 하면 이전 세션 모듈이 sys.modules 에 남아, 거기 없는 새
    # 함수를 임포트하는 순간 ImportError 로 죽는다 (maxbridge 에 이어 library 의
    # load_shelf 에서 실제로 두 번째로 터졌다). compat 과 _session 은 제외 —
    # Qt 바인딩과 살아 있는 창 핸들은 다시 읽으면 안 된다.
    from maxmcp.helpers import artoke_sync, github_sync
    from maxmcp.helpers import bvh as bvh_helpers
    from maxmcp.ui.studio import bridge as bridge_module
    from maxmcp.ui.studio import library, maxbridge, skeleton, thumb, timemap, video_jobs

    for module in (
        bvh_helpers,
        github_sync,
        artoke_sync,
        skeleton,
        timemap,
        library,
        thumb,
        video_jobs,
        maxbridge,
    ):
        importlib.reload(module)
    importlib.reload(bridge_module)
    return bridge_module.StudioBridge(_cache_dir())


def _replace_bridge(host: Any, bridge: Any) -> None:
    """새 메서드가 없는 이전 세션의 WebHost에서도 브리지를 교체한다."""
    old_bridge = host.bridge
    host._channel.deregisterObject(old_bridge)
    bridge.setParent(host)
    host.bridge = bridge
    host._channel.registerObject("bridge", bridge)
    old_bridge.deleteLater()


def launch() -> Any:
    if _session.window is not None:
        _session.window.show()
        _session.window.raise_()
        _replace_bridge(_session.window, _new_bridge())
        _session.window.load_page(PAGE)  # 코드가 바뀌었을 수 있으니 페이지는 새로 읽는다
        return _session.window

    from maxmcp.ui.studio.compat import max_main_window
    from maxmcp.ui.studio.webhost import WebHost

    bridge = _new_bridge()
    host = WebHost(bridge, title="BVH Studio", parent=max_main_window())
    host.show()
    host.raise_()
    host.load_page(PAGE)
    _session.window = host
    return host


if __name__ == "__main__":
    launch()
