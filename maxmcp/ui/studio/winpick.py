"""Pure widget-selection logic (no Qt imports, testable without PySide)."""

from typing import Any, Iterable, Optional


def _pick_max_window(widgets: Iterable[Any]) -> Optional[object]:
    """Pure helper: select Max main window from widget iterable.

    First pass: look for QmaxApplicationWindow (Max 2024).
    Second pass: any QMainWindow with no parent (fallback).

    Never raises. Handles deleted C++ objects, null metaObject gracefully.
    Returns None if no suitable window found.
    """
    # First pass: look for QmaxApplicationWindow
    for widget in widgets:
        try:
            if widget.parent() is not None:
                continue
            if widget.metaObject().className() == "QmaxApplicationWindow":
                return widget
        except Exception:
            # Deleted C++ object, null metaObject, etc. — skip it
            continue

    # Second pass: any QMainWindow with no parent
    for widget in widgets:
        try:
            if widget.parent() is None and widget.isWindow() and widget.inherits("QMainWindow"):
                return widget
        except Exception:
            # Same robustness
            continue

    return None
