"""후처리 다리. 핵심은 둘 — 실패해도 워커가 안 멈춘다, 프레임 수는 원본과 같다."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maxmcp.worker import postprocess_bridge as bridge

FIX = Path(r"C:\work\Ai\pose-prior\fixtures\two-people.rtmw3d.json.gz")


def test_falls_back_when_postprocess_fails(tmp_path, monkeypatch):
    # pose-prior 가 없거나 깨져도 원본 BVH 는 나가야 한다. 워커가 멈추면
    # 대기열이 통째로 선다.
    monkeypatch.setattr(bridge, "_load", lambda: (_ for _ in ()).throw(ImportError("no pose-prior")))
    out = tmp_path / "m.bvh"
    frames = bridge.convert_with_postprocess(FIX, out)
    assert out.is_file() and frames == 193
    report = json.loads(out.with_suffix(".postprocess.json").read_text(encoding="utf-8"))
    assert report["applied"] is False and "ImportError" in report["error"]


@pytest.mark.skipif(not FIX.is_file(), reason="fixture 없음")
def test_keeps_frame_count_and_reports_metrics(tmp_path):
    # 워커 모드는 대상 전환을 자르지 않는다 — 게시 단계의 frame count 검사와
    # rtmw3d_json 의 193 프레임에 맞아야 한다. 대신 전환 프레임을 보고한다.
    out = tmp_path / "m.bvh"
    frames = bridge.convert_with_postprocess(FIX, out)
    assert frames == 193
    text = out.read_text(encoding="utf-8")
    assert "Frames: 193" in text
    report = json.loads(out.with_suffix(".postprocess.json").read_text(encoding="utf-8"))
    assert report["applied"] is True
    assert 176 in report["target_switch_frames"]
    assert report["metrics"]["jitter"] < 3.0        # 원본은 9.3
