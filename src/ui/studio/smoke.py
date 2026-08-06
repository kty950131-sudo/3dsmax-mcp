"""Task 9 게이트 — Max 안에서 웹뷰가 실제로 뜨는지 증명하고 결과를 파일로 남긴다.

눈으로만 보면 "흰 화면" 을 놓치기 쉽다. 창을 PNG 로 캡처하고 색 분포까지 세어
비어 있는지를 기계가 판정한다. MCP 브리지 없이 스크립트 한 번만 실행해도
바깥에서 결과를 읽을 수 있게 하려는 것이다.

``run()`` 은 즉시 돌아온다. 페이지 로드가 비동기라 보고서는 몇 초 뒤에 쓰인다.
"""

import json
import os
import traceback
from typing import Any, Optional

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
REPORT_DIR = os.path.join(_REPO_ROOT, ".smoke")
REPORT_PATH = os.path.join(REPORT_DIR, "report.json")
SHOT_PATH = os.path.join(REPORT_DIR, "view.png")

# 창을 붙잡아 둔다. 지역 변수로만 두면 파이썬이 즉시 수거해 창이 사라진다.
_HOST: Optional[Any] = None


def _write(report: dict[str, Any]) -> None:
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)


def _image_stats(image: Any) -> dict[str, Any]:
    """캡처 이미지의 색 분포. 색이 거의 없으면 빈 화면으로 본다."""
    width, height = image.width(), image.height()
    if width <= 0 or height <= 0:
        return {"width": width, "height": height, "looks_blank": True}
    step = max(1, min(width, height) // 60)
    counts: dict[int, int] = {}
    for y in range(0, height, step):
        for x in range(0, width, step):
            pixel = image.pixel(x, y)
            counts[pixel] = counts.get(pixel, 0) + 1
    total = sum(counts.values())
    dominant = max(counts.values()) if counts else 0
    share = (dominant / total) if total else 1.0
    return {
        "width": width,
        "height": height,
        "sampled": total,
        "distinct_colors": len(counts),
        "dominant_share": round(share, 3),
        # 색이 두 종류 이하거나 한 색이 98% 이상이면 사실상 빈 화면이다
        "looks_blank": len(counts) <= 2 or share > 0.98,
    }


def run(settle_ms: int = 2500) -> Any:
    """스모크 창을 띄우고, settle_ms 뒤에 결과를 캡처해 보고서를 쓴다."""
    global _HOST
    report: dict[str, Any] = {"stage": "starting", "report_path": REPORT_PATH}

    try:
        from src.ui.studio.compat import BINDING, QtCore, max_main_window

        report["binding"] = BINDING
        report["qt_version"] = QtCore.qVersion()
        _write(report)
    except Exception:
        report["stage"] = "compat_import_failed"
        report["error"] = traceback.format_exc()
        _write(report)
        return None

    try:
        from src.ui.studio.bridge import StudioBridge
        from src.ui.studio.webhost import WebHost

        report["stage"] = "webengine_import_ok"
        _write(report)
    except Exception as exc:
        report["stage"] = "webengine_import_failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["trace"] = traceback.format_exc()
        _write(report)
        return None

    try:
        # 창을 닫고 새로 만들지 않는다. QWebEngineView 를 한 Max 세션에서 반복
        # 생성/파괴하면 Max 가 죽는다(실측: 세 번째 시도에서 프로세스 종료).
        # 이미 있으면 그대로 다시 띄우고 페이지만 새로 읽는다.
        if _HOST is not None:
            _HOST.show()
            _HOST.raise_()
            _HOST.load_page("smoke.html")
            report["stage"] = "reused_existing_host"
            _write(report)
            return _HOST

        cache_dir = os.path.join(REPORT_DIR, "cache")
        bridge = StudioBridge(cache_dir)
        host = WebHost(bridge, title="BVH Studio — smoke", parent=max_main_window())
        _HOST = host
        report["runtime_paths"] = getattr(host, "runtime_paths", None)
        _write(report)

        def on_loaded(ok: bool) -> None:
            report["load_finished_ok"] = bool(ok)
            _write(report)
            QtCore.QTimer.singleShot(settle_ms, capture)

        def capture() -> None:
            try:
                pixmap = host.view.grab()
                os.makedirs(REPORT_DIR, exist_ok=True)
                pixmap.save(SHOT_PATH, "PNG")
                report["screenshot"] = SHOT_PATH
                report["image"] = _image_stats(pixmap.toImage())
                _write(report)

                def got(value: Any) -> None:
                    try:
                        report["page"] = json.loads(value) if value else None
                    except Exception:
                        report["page_raw"] = str(value)
                    page = report.get("page") or {}
                    image = report.get("image") or {}
                    report["verdict"] = (
                        "PASS"
                        if (
                            report.get("load_finished_ok")
                            and page.get("ping_ok")
                            and page.get("canvas_drawn")
                            and not image.get("looks_blank", True)
                        )
                        else "FAIL"
                    )
                    report["stage"] = "done"
                    _write(report)

                # PySide6 6.5 는 (script, worldId, callback) 3인자만 받는다
                host.view.page().runJavaScript("window.__smokeResult || ''", 0, got)
            except Exception:
                report["stage"] = "capture_failed"
                report["error"] = traceback.format_exc()
                _write(report)

        host.view.loadFinished.connect(on_loaded)
        host.show()
        host.raise_()
        host.load_page("smoke.html")
        report["stage"] = "window_shown"
        _write(report)
        return host
    except Exception:
        report["stage"] = "construct_failed"
        report["error"] = traceback.format_exc()
        _write(report)
        return None
