"""PySide2(Max 2024) / PySide6(Max 2026) 차이를 흡수한다.

qtmax 는 Max 2026 에만 있다. 2024 의 site-packages 에는 pymxs, PySide2,
shiboken2 뿐이므로 부모 윈도우 탐색에 qtmax 를 전제할 수 없다.
"""

from typing import Optional

try:
    from PySide6 import QtCore, QtGui, QtWidgets

    BINDING = "PySide6"
except ImportError:  # pragma: no cover - 바인딩에 따라 갈린다
    try:
        from PySide2 import QtCore, QtGui, QtWidgets

        BINDING = "PySide2"
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "PySide2/PySide6 를 찾을 수 없습니다. 이 모듈은 3ds Max 안에서 실행해야 합니다."
        ) from exc

__all__ = ["QtCore", "QtGui", "QtWidgets", "BINDING", "max_main_window"]


def max_main_window() -> Optional[object]:
    """Max 메인 윈도우를 찾는다. 못 찾으면 None (부모 없이 띄운다)."""
    try:
        import qtmax  # Max 2026

        return qtmax.GetQMaxMainWindow()
    except Exception:
        pass

    app = QtWidgets.QApplication.instance()
    if app is None:
        return None
    for widget in app.topLevelWidgets():
        if widget.parent() is not None:
            continue
        if widget.metaObject().className() == "QmaxApplicationWindow":
            return widget
    for widget in app.topLevelWidgets():
        if widget.parent() is None and widget.isWindow() and widget.inherits("QMainWindow"):
            return widget
    return None
