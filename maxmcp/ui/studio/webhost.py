"""QWebEngineView 호스트. QWebChannel 로 파이썬 브리지를 JS 에 붙인다.

Max 2026 전용이다. 2024 의 PySide2 에는 QtWebEngine 모듈 자체가 없다.
"""

import os
from typing import Any, Optional

from maxmcp.ui.studio.compat import QtCore, QtWidgets

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# Max 2026 이 QtWebEngine 런타임을 두는 위치 (설치 루트 기준 상대 경로)
_ENGINE_RUNTIME = {
    "QTWEBENGINEPROCESS_PATH": ("QtWebEngineProcess.exe", True),
    "QTWEBENGINE_RESOURCES_PATH": ("resources", False),
    "QTWEBENGINE_LOCALES_PATH": (
        os.path.join("qt", "translations", "qtwebengine_locales"),
        False,
    ),
}


def configure_webengine_paths(root: Optional[str] = None) -> dict[str, str]:
    """Qt 가 QtWebEngine 런타임을 찾도록 재배치 환경변수를 잡는다.

    Max 2026 은 ``QtWebEngineProcess.exe`` 와 ``resources`` 를 **설치 루트**에 두는데
    Qt 의 ``LibraryExecutablesPath`` 는 ``bin/`` 을 가리킨다. 그래서 기본 탐색으로는
    헬퍼 프로세스를 못 찾고, **창은 뜨지만 페이지가 영원히 로드되지 않는다**
    (실측: ``loadFinished`` 가 38초간 오지 않음).

    구성 요소가 빠진 게 아니라 경로가 어긋난 것이므로, Qt 가 이런 재배치 배포를
    위해 공식 지원하는 환경변수로 실제 위치를 알려준다. 웹엔진이 렌더 프로세스를
    띄우기 전에 호출해야 한다.
    """
    root = root or QtCore.QCoreApplication.applicationDirPath()
    applied: dict[str, str] = {}
    missing: list[str] = []
    for var, (relative, is_file) in _ENGINE_RUNTIME.items():
        target = os.path.join(root, relative)
        found = os.path.isfile(target) if is_file else os.path.isdir(target)
        if found:
            os.environ[var] = target
            applied[var] = target
        else:
            missing.append(target)
    if missing:
        raise RuntimeError(
            "QtWebEngine 런타임을 찾을 수 없습니다. 없는 경로: " + ", ".join(missing)
        )
    return applied


def import_webengine() -> tuple[Any, Any, Any, Any]:
    """QtWebEngine 을 가져온다. 없으면 사람이 읽을 수 있는 메시지로 바꾼다."""
    try:
        from PySide6.QtWebChannel import QWebChannel
        from PySide6.QtWebEngineCore import QWebEngineScript, QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError as exc:
        raise ImportError(
            "QtWebEngine 을 찾을 수 없습니다. BVH Studio 는 3ds Max 2026 이 필요합니다. "
            "Max 2024 의 PySide2 에는 QtWebEngine 이 없으므로, 2024 에서는 기존 "
            "maxscript/bvh_biped_ui.ms 롤아웃을 쓰세요."
        ) from exc
    return QWebChannel, QWebEngineScript, QWebEngineSettings, QWebEngineView


def qwebchannel_js() -> str:
    """Qt 내장 리소스에서 qwebchannel.js 원문을 읽는다.

    ``file://`` 페이지에서 ``qrc:///`` 스크립트를 ``<script src>`` 로 부르면 스킴
    제약에 걸려 조용히 실패한다. 내용을 읽어 문서 생성 시점에 주입하면 그 문제를
    피하고, 페이지가 별도 파일에 의존하지도 않는다.
    """
    handle = QtCore.QFile(":/qtwebchannel/qwebchannel.js")
    if not handle.open(QtCore.QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError("qwebchannel.js 리소스를 열 수 없습니다")
    try:
        return bytes(handle.readAll()).decode("utf-8")
    finally:
        handle.close()


class WebHost(QtWidgets.QWidget):
    """웹뷰 한 장을 담은 최상위 창."""

    def __init__(
        self,
        bridge: QtCore.QObject,
        title: str = "BVH Studio",
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        # 웹뷰를 만들기 전에 런타임 경로를 잡아야 한다
        self.runtime_paths = configure_webengine_paths()
        QWebChannel, QWebEngineScript, QWebEngineSettings, QWebEngineView = (
            import_webengine()
        )

        self.setWindowTitle(title)
        self.setWindowFlag(QtCore.Qt.WindowType.Window, True)
        self.resize(1400, 900)

        self.view = QWebEngineView(self)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

        settings = self.view.settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )

        script = QWebEngineScript()
        script.setName("qwebchannel")
        script.setSourceCode(qwebchannel_js())
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        self.view.page().scripts().insert(script)

        # QWebChannel.registerObject 는 소유권을 가져가지 않는다. 호출자가 브리지를
        # 지역 변수로만 들고 있으면 파이썬이 수거하면서 C++ QObject 까지 파괴되고,
        # JS 쪽에는 껍데기만 남아 슬롯 호출이 응답 없이 사라진다(실측: 채널은
        # connected 인데 ping 콜백이 영영 안 불림). 여기서 부모로 잡아 붙든다.
        bridge.setParent(self)
        self.bridge = bridge

        self._channel = QWebChannel(self)
        self._channel.registerObject("bridge", bridge)
        self.view.page().setWebChannel(self._channel)

    def replace_bridge(self, bridge: QtCore.QObject) -> None:
        """웹뷰를 재생성하지 않고 실행 중인 Python 브리지만 교체한다."""
        old_bridge = self.bridge
        self._channel.deregisterObject(old_bridge)
        bridge.setParent(self)
        self.bridge = bridge
        self._channel.registerObject("bridge", bridge)
        old_bridge.deleteLater()

    def load_page(self, filename: str) -> None:
        path = os.path.join(WEB_DIR, filename)
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        self.view.setUrl(QtCore.QUrl.fromLocalFile(path))
