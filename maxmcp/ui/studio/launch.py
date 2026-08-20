"""3ds Max 안에서 BVH Studio 창을 띄운다.

창은 **세션당 한 번만 만든다.** QWebEngineView 를 반복 생성/파괴하면 Max 가
죽는다 (Task 9 실측 — 세 번째 재생성에서 프로세스 종료). 다시 실행하면 기존
창을 앞으로 올리고 페이지만 새로 읽는다.
"""

import os
import sys
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


# 다시 읽으면 안 되는 모듈. Qt 바인딩과 살아 있는 창 핸들이라, 새로 읽으면
# 이전 창을 가리키던 참조가 끊긴다.
_KEEP = ("maxmcp.ui.studio.compat", "maxmcp.ui.studio._session")


def _forget_maxmcp_modules(modules: dict) -> list[str]:
    """``maxmcp`` 모듈을 캐시에서 지운다. 지운 이름을 돌려준다.

    이전에는 모듈 목록을 손으로 의존 순서대로 적어 두고 ``importlib.reload`` 를
    돌렸다. 그 순서는 세 번 깨졌다 — 새 의존이 생길 때마다(``blend``→``quat``,
    ``library``→``load_shelf``, ``bvh``→``quat_mul``) 목록을 같이 고쳐야 하는데,
    안 고치면 낡은 모듈을 상대로 새 이름을 임포트하다 ImportError 로 죽는다.
    Max 안에서만, 그것도 다음 실행에서 터지므로 여기서는 안 보인다.

    캐시에서 지우면 다음 임포트가 의존 순서를 알아서 맞춘다 — 지킬 불변식이
    사라지는 쪽이 지키기 쉬운 불변식보다 낫다.
    """
    doomed = [
        name
        for name in list(modules)
        if (name == "maxmcp" or name.startswith("maxmcp.")) and name not in _KEEP
    ]
    for name in doomed:
        del modules[name]
    return doomed


def _new_bridge() -> Any:
    _forget_maxmcp_modules(sys.modules)
    from maxmcp.ui.studio import bridge as bridge_module

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
