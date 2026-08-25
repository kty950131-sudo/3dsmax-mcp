"""RTMW3D 결과를 후처리해 BVH 로 내는 converter. MotionPipeline 의 converter 자리에 꽂는다.

후처리는 pose-prior 가 든다(`C:/work/Ai/pose-prior/postprocess.py`). 그쪽 코드를
여기로 복사하지 않는 이유: 파이프라인과 측정 도구와 회귀 시험이 한 폴더에
있어야 같은 잣대가 유지된다. 이 파일은 그 폴더를 경로에 얹고 부르는 다리다.

후처리가 실패하면 원래 변환기로 물러난다. 후처리는 품질을 올리는 것이지
없으면 안 되는 것이 아니다 — 워커가 멈추는 것보다 원본 BVH 라도 나가는 게 낫다.
실패는 trace 에 남겨 나중에 볼 수 있게 한다.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from maxmcp.rtmw3d.motion import convert_rtmw3d_file

POSE_PRIOR_DIR = Path(os.environ.get("ARTOKE_POSE_PRIOR_DIR", r"C:\work\Ai\pose-prior"))


def _load():
    if str(POSE_PRIOR_DIR) not in sys.path:
        sys.path.insert(0, str(POSE_PRIOR_DIR))
    import postprocess  # noqa: WPS433
    import measure_tracking  # noqa: WPS433
    return postprocess, measure_tracking


def convert_with_postprocess(source: str | Path, output: str | Path) -> int:
    """convert_rtmw3d_file 과 같은 서명. 옆에 `<output>.postprocess.json` 을 남긴다."""
    source, output = Path(source), Path(output)
    report_path = output.with_suffix(".postprocess.json")
    try:
        pp, mt = _load()
        log = pp.run(source, output, cut_switches=False)
        metrics = mt.measure(output)
        report = {
            "applied": True,
            "pipeline": {k: log[k] for k in ("smooth_sigma", "prior_penalty", "rot_sigma", "rom_clamped") if k in log},
            "target_switch_frames": log.get("target_switch_frames", []),
            # 추적을 놓쳐 마지막 자세로 채운 구간 — UI 타임라인이 주황으로 표시한다(08-25).
            "lost_segments": log.get("lost_segments", []),
            "gated_frames": {
                "low_confidence": log.get("low_confidence_frames", 0),
                "bone_outlier": log.get("bone_outlier_frames", 0),
            },
            "foot_plant": log.get("foot_plant"),
            "metrics": {
                "jitter": metrics["jitter"]["mean"],
                "foot_slide": _mean_slide(metrics),
                "wrist_change": {k: v["elbow_angle_frame_change"] for k, v in metrics["wrist"].items()},
            },
        }
        frames = log["frames_kept"] if "frames_kept" in log else log["frames"]
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 원본으로 물러난다
        frames = convert_rtmw3d_file(_plain_json(source), output)
        report = {"applied": False, "error": f"{type(exc).__name__}: {exc}"}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(frames)


def _mean_slide(metrics: dict) -> float | None:
    vals = [v["slide"] for v in metrics["foot_slide"].values() if v.get("slide") is not None]
    return float(sum(vals) / len(vals)) if vals else None


def _plain_json(source: Path) -> Path:
    """원래 변환기는 평문 JSON 만 읽는다. 게시본은 gzip 이라 폴백 때 풀어 준다."""
    if source.suffix != ".gz":
        return source
    import gzip
    plain = source.with_suffix("")
    plain.write_bytes(gzip.open(source, "rb").read())
    return plain
