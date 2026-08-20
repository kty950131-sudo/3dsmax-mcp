"""창을 닫아도 남아야 하는 UI 상태.

`bridge.py` 의 `read_settings` / `write_setting` 이 부르는 유일한 소비자다.
화면 쪽 계약(`studio_draft.html` 의 `settingGet` / `settingSet`):

* `load()` 는 **평평한 dict** 를 돌려준다. 키는 화면이 정하고("bvhStudio.folders",
  "bvhStudio.curvesOpen" 등) 값은 JSON 이 담을 수 있는 것이면 무엇이든 된다.
* `save()` 는 키 하나만 바꾼다. 화면이 저장을 기다리지 않고 메모리에 먼저
  반영하므로(`settingSet` 주석), 여기서 느려지거나 던져도 그 세션은 굴러간다.

읽기가 실패해도 예외를 올리지 않고 빈 dict 를 준다. 설정 파일 하나가 깨졌다고
스튜디오가 안 뜨면 사용자가 고칠 방법이 없다 — 그 파일은 UI 편의값일 뿐이다.
"""

import json
import os
import tempfile
from typing import Any

FILENAME = "settings.json"


def _path(cache_dir: str) -> str:
    return os.path.join(cache_dir, FILENAME)


def load(cache_dir: str) -> dict[str, Any]:
    """저장된 설정 전부. 없거나 깨졌으면 빈 dict."""
    try:
        with open(_path(cache_dir), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    # 최상위가 dict 가 아니면 화면의 `SETTINGS.data[key]` 가 성립하지 않는다.
    return data if isinstance(data, dict) else {}


def save(cache_dir: str, key: str, value: Any) -> dict[str, Any]:
    """키 하나를 저장하고 갱신된 설정 전부를 돌려준다.

    같은 파일에 여러 키가 들어가므로 **읽고-바꾸고-쓴다**. 통째로 덮으면 다른
    키가 사라진다.

    쓰기는 임시 파일에 쓴 뒤 바꿔치기한다. 그냥 열어 쓰면 도중에 죽었을 때 반쪽
    JSON 이 남고, 다음 부팅에서 설정이 통째로 날아간다.
    """
    data = load(cache_dir)
    data[key] = value

    os.makedirs(cache_dir, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=cache_dir, prefix=FILENAME, suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(handle.name, _path(cache_dir))
    except OSError:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    return data
