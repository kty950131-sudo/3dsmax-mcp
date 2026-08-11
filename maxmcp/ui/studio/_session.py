"""세션 앵커 — 리로드에도 살아남아야 하는 참조만 둔다.

런처(.ms)는 코드 갱신을 위해 launch 모듈을 importlib.reload 하는데, 그때
모듈 전역이 초기화된다. 웹뷰 창 참조가 거기 있으면 재실행마다 창을 새로
만들게 되고, 웹뷰 반복 생성은 Max 를 죽인다 (Task 9 실측). 이 모듈은
아무도 reload 하지 않으므로 창 참조가 세션 내내 유지된다.
"""

from typing import Any, Optional

window: Optional[Any] = None
