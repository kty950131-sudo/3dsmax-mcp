"""RTMW3D runtime discovery and subprocess command construction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rtmw3dReadiness:
    ready: bool
    environment: Path
    repository: Path
    checkpoint_dir: Path
    missing_files: tuple[str, ...]


def check_rtmw3d(environment: Path, repository: Path) -> Rtmw3dReadiness:
    """Return a deterministic file-level readiness report."""
    environment = environment.resolve()
    repository = repository.resolve()
    checkpoint_dir = repository.parent / "models" / "rtmw3d"
    required = (
        (environment / "Scripts" / "python.exe", "python.exe"),
        (
            repository / "projects" / "rtmpose3d" / "configs"
            / "rtmw3d-l_8xb64_cocktail14-384x288.py",
            "rtmw3d-l config",
        ),
        (
            checkpoint_dir / "rtmw3d-l_8xb64_cocktail14-384x288-794dbc78_20240626.pth",
            "rtmw3d-l checkpoint",
        ),
        (
            checkpoint_dir / "rtmdet_m_8xb32-100e_coco-obj365-person-235e8209.pth",
            "rtmdet checkpoint",
        ),
    )
    missing = tuple(label for path, label in required if not path.is_file())
    return Rtmw3dReadiness(not missing, environment, repository, checkpoint_dir, missing)


def default_readiness(project_root: Path | None = None) -> Rtmw3dReadiness:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    workspace = root.parent
    return check_rtmw3d(root / ".venv-rtmw3d", workspace / "vendor" / "mmpose")


def build_rtmw3d_command(
    video: Path, output: Path, runtime: Rtmw3dReadiness
) -> list[str]:
    """Build the extractor command without shell interpolation."""
    project_root = Path(__file__).resolve().parents[2]
    return [
        str(runtime.environment / "Scripts" / "python.exe"),
        str(project_root / "scripts" / "run-rtmw3d.py"),
        "--mmpose-repo",
        str(runtime.repository),
        "--checkpoint-dir",
        str(runtime.checkpoint_dir),
        "--input",
        str(video),
        "--output",
        str(output),
    ]
