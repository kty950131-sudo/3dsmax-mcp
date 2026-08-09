"""Non-destructive 2D corrections for one RTMW3D motion file."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import tempfile
from typing import Callable, Literal

from src.rtmw3d.motion import BODY23_NAMES, Rtmw3dMotion, convert_rtmw3d_file, load_rtmw3d


EditKind = Literal["manual", "copied", "propagated"]


@dataclass(frozen=True)
class PointEdit:
    x: float
    y: float
    kind: EditKind


class TrackingSession:
    def __init__(
        self,
        source_path: Path,
        motion: Rtmw3dMotion,
        payload: dict,
        converter: Callable[[Path, Path], int] = convert_rtmw3d_file,
    ) -> None:
        self.source_path = source_path
        self.motion = motion
        self._payload = payload
        self._converter = converter
        self._edits: dict[tuple[int, str], PointEdit] = {}

    @classmethod
    def open(
        cls,
        source_path: str | Path,
        converter: Callable[[Path, Path], int] = convert_rtmw3d_file,
    ) -> "TrackingSession":
        path = Path(source_path).resolve()
        motion = load_rtmw3d(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(path, motion, payload, converter)

    @property
    def dirty(self) -> bool:
        return bool(self._edits)

    def frame(self, frame_index: int) -> dict:
        self._validate_frame(frame_index)
        source_frame = self.motion.frames[frame_index]
        keypoints = {
            name: list(source_frame.keypoints[index])
            for index, name in enumerate(BODY23_NAMES)
        }
        if source_frame.image_keypoints is None:
            image_keypoints = {
                name: [point[0], -point[1]] for name, point in keypoints.items()
            }
        else:
            image_keypoints = {
                name: list(source_frame.image_keypoints[index])
                for index, name in enumerate(BODY23_NAMES)
            }
        edit_kinds = {}
        for name in BODY23_NAMES:
            edit = self._edits.get((frame_index, name))
            if edit is None:
                continue
            image_keypoints[name] = [edit.x, edit.y]
            keypoints[name][0] = edit.x
            keypoints[name][1] = -edit.y
            edit_kinds[name] = edit.kind
        return {
            "index": frame_index,
            "keypoints": keypoints,
            "image_keypoints": image_keypoints,
            "scores": {
                name: source_frame.scores[index]
                for index, name in enumerate(BODY23_NAMES)
            },
            "edit_kinds": edit_kinds,
        }

    def set_point(
        self, frame_index: int, joint: str, x: float, y: float
    ) -> dict:
        return self._set_edit(frame_index, joint, x, y, "manual")

    def copy_to_next(self, frame_index: int, joint: str) -> dict:
        self._validate_frame(frame_index)
        next_index = frame_index + 1
        self._validate_frame(next_index)
        point = self.frame(frame_index)["image_keypoints"][self._validate_joint(joint)]
        return self._set_edit(next_index, joint, point[0], point[1], "copied")

    def propagate(self, frame_index: int, end_frame: int, joint: str) -> dict:
        self._validate_frame(frame_index)
        self._validate_frame(end_frame)
        name = self._validate_joint(joint)
        if end_frame <= frame_index:
            raise ValueError("end_frame must be after frame_index")
        source_start = self._source_image_point(frame_index, name)
        edited_start = self.frame(frame_index)["image_keypoints"][name]
        offset_x = edited_start[0] - source_start[0]
        offset_y = edited_start[1] - source_start[1]
        updated = []
        for index in range(frame_index + 1, end_frame + 1):
            existing = self._edits.get((index, name))
            if existing is not None and existing.kind == "manual":
                return {"updated": updated, "stopped_at": index}
            weight = (end_frame - index) / (end_frame - frame_index)
            source = self._source_image_point(index, name)
            self._set_edit(
                index,
                name,
                source[0] + offset_x * weight,
                source[1] + offset_y * weight,
                "propagated",
            )
            updated.append(index)
        return {"updated": updated, "stopped_at": None}

    def reset_point(self, frame_index: int, joint: str) -> dict:
        self._validate_frame(frame_index)
        name = self._validate_joint(joint)
        removed = self._edits.pop((frame_index, name), None) is not None
        return {"frame": frame_index, "joint": name, "removed": removed}

    def save(self, library: str | Path) -> dict:
        if self.motion.image_size is None:
            raise RuntimeError("legacy RTMW3D data must be re-extracted before editing")
        target_dir = Path(library).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = self.source_path.stem
        edited_json = target_dir / f"{stem}_edited.json"
        edited_bvh = target_dir / f"{stem}_edited_tpose.bvh"
        trace_path = target_dir / f"{stem}_edited_trace.json"
        payload = deepcopy(self._payload)
        for (frame_index, joint), edit in self._edits.items():
            frame = payload["frames"][frame_index]
            frame["image_keypoints"][joint] = [edit.x, edit.y]
            frame["keypoints"][joint][0] = edit.x
            frame["keypoints"][joint][1] = -edit.y
        payload["edited_from"] = str(self.source_path)
        payload["edited_at"] = datetime.now(timezone.utc).isoformat()
        payload["edit_count"] = len(self._edits)
        _write_json_atomic(edited_json, payload)
        temp_bvh = _temporary_path(edited_bvh)
        try:
            frame_count = self._converter(edited_json, temp_bvh)
            temp_bvh.replace(edited_bvh)
        finally:
            temp_bvh.unlink(missing_ok=True)
        trace = {
            "stage": "tracking_editor",
            "source_rtmw3d": str(self.source_path),
            "edited_rtmw3d": str(edited_json),
            "bvh": str(edited_bvh),
            "edit_count": len(self._edits),
            "frame_count": frame_count,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sha256": {
                "source": _sha256(self.source_path),
                "edited": _sha256(edited_json),
                "bvh": _sha256(edited_bvh),
            },
        }
        _write_json_atomic(trace_path, trace)
        return {
            "rtmw3d_path": str(edited_json),
            "bvh_path": str(edited_bvh),
            "trace_path": str(trace_path),
            "edit_count": len(self._edits),
            "frame_count": frame_count,
        }

    def _set_edit(
        self,
        frame_index: int,
        joint: str,
        x: float,
        y: float,
        kind: EditKind,
    ) -> dict:
        self._validate_frame(frame_index)
        name = self._validate_joint(joint)
        point_x, point_y = float(x), float(y)
        if not math.isfinite(point_x) or not math.isfinite(point_y):
            raise ValueError("tracking coordinates must be finite")
        self._edits[(frame_index, name)] = PointEdit(point_x, point_y, kind)
        return {
            "frame": frame_index,
            "joint": name,
            "x": point_x,
            "y": point_y,
            "kind": kind,
        }

    def _source_image_point(self, frame_index: int, joint: str) -> tuple[float, float]:
        frame = self.motion.frames[frame_index]
        index = BODY23_NAMES.index(joint)
        if frame.image_keypoints is not None:
            return frame.image_keypoints[index]
        point = frame.keypoints[index]
        return point[0], -point[1]

    def _validate_frame(self, frame_index: int) -> None:
        if not isinstance(frame_index, int) or not 0 <= frame_index < len(self.motion.frames):
            raise IndexError(f"frame index is out of range: {frame_index}")

    @staticmethod
    def _validate_joint(joint: str) -> str:
        if joint not in BODY23_NAMES:
            raise ValueError(f"unknown joint: {joint}")
        return joint


def _temporary_path(target: Path) -> Path:
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{target.stem}.", suffix=target.suffix, dir=target.parent, delete=False
    )
    handle.close()
    return Path(handle.name)


def _write_json_atomic(target: Path, payload: dict) -> None:
    temporary = _temporary_path(target)
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
