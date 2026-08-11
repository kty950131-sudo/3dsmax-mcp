"""Tests for maxmcp/helpers/github_sync.py (gh calls are monkeypatched)."""
import json

from maxmcp.helpers import github_sync


REMOTE = [
    {"name": "spin-kick.bvh", "size": 100},
    {"name": "run-jump.bvh", "size": 200},
]


def test_plan_sync_downloads_missing_files():
    todo = github_sync.plan_sync(REMOTE, {})
    assert todo == REMOTE


def test_plan_sync_skips_up_to_date_files():
    local = {"artoke_spin-kick.bvh": 100, "artoke_run-jump.bvh": 200}
    assert github_sync.plan_sync(REMOTE, local) == []


def test_plan_sync_redownloads_on_size_change():
    local = {"artoke_spin-kick.bvh": 999, "artoke_run-jump.bvh": 200}
    assert github_sync.plan_sync(REMOTE, local) == [REMOTE[0]]


def test_plan_sync_ignores_unprefixed_local_files():
    local = {"spin-kick.bvh": 100, "run-jump.bvh": 200}
    assert github_sync.plan_sync(REMOTE, local) == REMOTE


def test_list_remote_motions_filters_bvh_files(monkeypatch):
    api = [
        {"name": "spin-kick.bvh", "size": 100, "type": "file"},
        {"name": "readme.md", "size": 10, "type": "file"},
        {"name": "old", "size": 0, "type": "dir"},
        {"name": "Pirouette.BVH", "size": 50, "type": "file"},
    ]
    monkeypatch.setattr(github_sync, "_run_gh", lambda args: json.dumps(api).encode())
    assert github_sync.list_remote_motions() == [
        {"name": "spin-kick.bvh", "size": 100},
        {"name": "Pirouette.BVH", "size": 50},
    ]


def test_sync_motions_downloads_with_prefix(tmp_path, monkeypatch):
    (tmp_path / "artoke_run-jump.bvh").write_bytes(b"x" * 200)

    def fake_run_gh(args):
        if args[1].startswith("repos/") and "Accept" not in args:
            return json.dumps([{**e, "type": "file"} for e in REMOTE]).encode()
        return b"HIERARCHY"

    monkeypatch.setattr(github_sync, "_run_gh", fake_run_gh)
    res = github_sync.sync_motions(str(tmp_path))
    assert res == {"downloaded": ["artoke_spin-kick.bvh"], "remote_total": 2}
    assert (tmp_path / "artoke_spin-kick.bvh").read_bytes() == b"HIERARCHY"


def test_sync_motions_reports_gh_errors(tmp_path, monkeypatch):
    def fail(args):
        raise RuntimeError("gh: Not Found (HTTP 404)")

    monkeypatch.setattr(github_sync, "_run_gh", fail)
    try:
        github_sync.sync_motions(str(tmp_path))
    except RuntimeError as exc:
        assert "404" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
