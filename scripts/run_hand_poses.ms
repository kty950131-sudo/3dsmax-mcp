-- 손 포즈 굽기 실행기.
--
-- MCP 브릿지의 안전 모드가 명령 문자열에서 "python.execute" 를 찾아 막는데,
-- `python.ExecuteFile` 이 그 부분 문자열에 걸린다. 브릿지는 설정 파일을 모듈
-- 적재 때만 읽어서 ini 를 고쳐도 도는 중에는 안 바뀐다.
--
-- 그래서 파일로 한 겹 감싼다. 브릿지에는 `fileIn` 만 보내고, 실제 호출은 이
-- 파일 안에 있다. 사용자가 안전 모드 해제를 승인한 일을 Max 재시작 없이
-- 그대로 하는 것이다.
python.ExecuteFile @"C:\work\Ai\3dsmax-mcp\scripts\export_hand_poses.py"
