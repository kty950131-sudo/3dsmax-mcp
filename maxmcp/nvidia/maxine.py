"""Readiness checks and command construction for NVIDIA Maxine Body Pose."""

from dataclasses import dataclass
import os
from pathlib import Path


REQUIRED_FILES = (
    "include/nvAR.h",
    "models",
    "artoke/maxine_body34.exe",
)
REQUIRED_FEATURES = (
    "nvarbodyposeestimation",
    "nvarbodydetection",
)


@dataclass(frozen=True)
class MaxineReadiness:
    ready: bool
    sdk_root: Path
    missing_files: tuple[str, ...]
    missing_features: tuple[str, ...]


def default_sdk_root() -> Path:
    """Return the official Windows install location unless explicitly overridden."""
    override = os.environ.get("NVAR_SDK_ROOT")
    if override:
        return Path(override)
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    return Path(program_files) / "NVIDIA Corporation" / "NVIDIA AR SDK"


def check_maxine(root: Path | None = None) -> MaxineReadiness:
    """Report every missing SDK component without attempting installation."""
    sdk_root = (root or default_sdk_root()).resolve()
    missing_files = tuple(
        relative for relative in REQUIRED_FILES if not (sdk_root / relative).exists()
    )
    missing_features = tuple(
        feature
        for feature in REQUIRED_FEATURES
        if not (sdk_root / "features" / feature / "bin").is_dir()
    )
    return MaxineReadiness(
        ready=not missing_files and not missing_features,
        sdk_root=sdk_root,
        missing_files=missing_files,
        missing_features=missing_features,
    )


def build_bodytrack_command(
    video: Path,
    output: Path,
    sdk_root: Path,
) -> list[str]:
    """Build the Artoke JSON extractor command without shell interpolation."""
    return [
        str(sdk_root / "artoke" / "maxine_body34.exe"),
        "--input",
        str(video),
        "--output",
        str(output),
    ]
