"""PySide2(Max 2024) / PySide6(Max 2026) 차이를 흡수한다.

qtmax 는 Max 2026 에만 있다. 2024 의 site-packages 에는 pymxs, PySide2,
shiboken2 뿐이므로 부모 윈도우 탐색에 qtmax 를 전제할 수 없다.
"""

from typing import Optional

from src.ui.studio.winpick import _pick_max_window

try:
    from PySide6 import QtCore, QtGui, QtWidgets

    BINDING = "PySide6"
except Exception as exc_6:  # pragma: no cover - 바인딩에 따라 갈린다
    try:
        from PySide2 import QtCore, QtGui, QtWidgets

        BINDING = "PySide2"
    except Exception as exc_2:  # pragma: no cover
        raise ImportError(
            "PySide2/PySide6 를 찾을 수 없습니다. 이 모듈은 3ds Max 안에서 실행해야 합니다."
        ) from exc_2

__all__ = ["QtCore", "QtGui", "QtWidgets", "BINDING", "max_main_window"]


def max_main_window() -> Optional[object]:
    """Max 메인 윈도우를 찾는다. 못 찾으면 None (부모 없이 띄운다).

    Never raises. Any failure returns None, leaving caller free to create unparented window.
    """
    try:
        import qtmax  # Max 2026

        return qtmax.GetQMaxMainWindow()
    except Exception:
        pass

    try:
        app = QtWidgets.QApplication.instance()
        if app is None:
            return None
        return _pick_max_window(app.topLevelWidgets())
    except Exception:
        # QApplication.instance() could fail, topLevelWidgets() could fail
        return None
