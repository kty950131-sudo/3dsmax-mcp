"""Sync BVH motion clips from a GitHub repository into the local library.

Called from maxscript/bvh_biped_ui.ms through Max's embedded Python. The gh
CLI supplies authentication, so private repositories work as long as
``gh auth login`` has been run for the owning account.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

DEFAULT_REPO = "kty950131-sudo/script-market"
DEFAULT_REMOTE_PATH = "public/motions"
DEFAULT_PREFIX = "artoke_"

# hide the console window gh would otherwise flash inside the Max GUI process
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_gh(args: list[str]) -> bytes:
    exe = shutil.which("gh")
    if exe is None:
        raise RuntimeError("gh CLI not found on PATH — install GitHub CLI and run 'gh auth login'")
    proc = subprocess.run([exe, *args], capture_output=True, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "ignore").strip()
        raise RuntimeError(err or f"gh {' '.join(args[:2])} failed")
    return proc.stdout


def list_remote_motions(
    repo: str = DEFAULT_REPO, remote_path: str = DEFAULT_REMOTE_PATH
) -> list[dict]:
    """Return [{"name", "size"}] for .bvh files in the repo folder."""
    raw = _run_gh(["api", f"repos/{repo}/contents/{remote_path}"])
    entries = json.loads(raw.decode("utf-8"))
    return [
        {"name": e["name"], "size": e["size"]}
        for e in entries
        if e.get("type") == "file" and e["name"].lower().endswith(".bvh")
    ]


def plan_sync(
    remote: list[dict], local_sizes: dict[str, int], prefix: str = DEFAULT_PREFIX
) -> list[dict]:
    """Remote entries whose local copy (prefix + name) is missing or differs in size."""
    return [e for e in remote if local_sizes.get(prefix + e["name"]) != e["size"]]


def download_motion(
    name: str,
    dest_file: Path,
    repo: str = DEFAULT_REPO,
    remote_path: str = DEFAULT_REMOTE_PATH,
) -> None:
    data = _run_gh(
        ["api", "-H", "Accept: application/vnd.github.raw", f"repos/{repo}/contents/{remote_path}/{name}"]
    )
    Path(dest_file).write_bytes(data)


def sync_motions(
    dest_dir: str,
    repo: str = DEFAULT_REPO,
    remote_path: str = DEFAULT_REMOTE_PATH,
    prefix: str = DEFAULT_PREFIX,
) -> dict:
    """Download new/changed .bvh files into dest_dir as <prefix><name>.

    Returns {"downloaded": [local names], "remote_total": int}.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    remote = list_remote_motions(repo, remote_path)
    local_sizes = {p.name: p.stat().st_size for p in dest.glob("*.bvh")}
    todo = plan_sync(remote, local_sizes, prefix)
    for entry in todo:
        download_motion(entry["name"], dest / (prefix + entry["name"]), repo=repo, remote_path=remote_path)
    return {"downloaded": [prefix + e["name"] for e in todo], "remote_total": len(remote)}
