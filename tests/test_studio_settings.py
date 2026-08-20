"""`maxmcp.ui.studio.settings` — 창을 닫아도 남는 UI 상태.

계약의 출처는 `studio_draft.html` 의 `settingGet` / `settingSet` 이다:
평평한 dict, 키 하나씩 저장, 실패해도 화면은 굴러간다.
"""

import json
import os

from maxmcp.ui.studio import settings


def test_load_returns_empty_when_there_is_nothing_yet(tmp_path) -> None:
    assert settings.load(str(tmp_path)) == {}


def test_save_then_load_round_trips(tmp_path) -> None:
    settings.save(str(tmp_path), "bvhStudio.curvesOpen", True)
    assert settings.load(str(tmp_path)) == {"bvhStudio.curvesOpen": True}


def test_save_keeps_the_other_keys(tmp_path) -> None:
    """한 파일에 여러 키가 들어간다 — 통째로 덮으면 나머지가 사라진다."""
    settings.save(str(tmp_path), "bvhStudio.folders", ["C:/a", "C:/b"])
    settings.save(str(tmp_path), "bvhStudio.curvesOpen", False)
    assert settings.load(str(tmp_path)) == {
        "bvhStudio.folders": ["C:/a", "C:/b"],
        "bvhStudio.curvesOpen": False,
    }


def test_save_overwrites_the_same_key(tmp_path) -> None:
    settings.save(str(tmp_path), "k", 1)
    assert settings.save(str(tmp_path), "k", 2)["k"] == 2


def test_load_survives_a_broken_file(tmp_path) -> None:
    """설정 하나가 깨졌다고 스튜디오가 안 뜨면 사용자가 고칠 방법이 없다."""
    (tmp_path / settings.FILENAME).write_text("{ this is not json", encoding="utf-8")
    assert settings.load(str(tmp_path)) == {}


def test_load_ignores_a_non_object_top_level(tmp_path) -> None:
    # 화면은 `SETTINGS.data[key]` 로 읽는다 — 배열이 오면 성립하지 않는다.
    (tmp_path / settings.FILENAME).write_text("[1, 2, 3]", encoding="utf-8")
    assert settings.load(str(tmp_path)) == {}


def test_save_creates_the_cache_folder(tmp_path) -> None:
    # 첫 실행에는 캐시 폴더 자체가 없다.
    target = tmp_path / "bvh_studio_cache"
    settings.save(str(target), "k", "v")
    assert settings.load(str(target)) == {"k": "v"}


def test_save_leaves_no_temp_files_behind(tmp_path) -> None:
    settings.save(str(tmp_path), "k", "v")
    assert os.listdir(tmp_path) == [settings.FILENAME]


def test_saved_file_is_readable_json(tmp_path) -> None:
    """사람이 열어 고칠 수 있어야 한다 — 손으로 지우는 것이 유일한 복구 수단이다."""
    settings.save(str(tmp_path), "한글키", "값")
    text = (tmp_path / settings.FILENAME).read_text(encoding="utf-8")
    assert "한글키" in text  # ensure_ascii=False
    assert json.loads(text) == {"한글키": "값"}
