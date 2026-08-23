-- bvh_studio2.ms — BVH Studio + RTMW3D 영상→Biped BVH 실행
--
-- 이 파일을 뷰포트에 드래그하거나 Scripting > Run Script 로 실행한다.
-- Max 2026 전용 (QtWebEngine). 2024 는 기존 bvh_biped_ui.ms 를 쓴다.
-- 상단 "영상 추가"에서 로컬 RTMW3D 관절 추정 작업을 시작할 수 있다.
-- 원본 bvh_biped_ui.ms 와 공존하며 로직은 전부 maxmcp/ 파이썬에 있다.
-- 상단 선반 칩 "손 포즈" / "포즈": assets/poses/hand_pose.cpy(손목+손가락)와
-- body_pose.cpy(전신)의 포즈를 카드로 보여준다. 카드를 누르면 대상 바이패드의
-- 현재 프레임·현재 레이어에 들어가고, "＋ 맥스에서 저장"은 지금 맥스에 서 있는
-- 자세를 그대로 카드로 만들어 그 .cpy 에 굽는다(카드 위 × 로 지운다).

(
	local thisFile = getThisScriptFilename()
	local repoRoot = if thisFile != undefined then
		(pathConfig.removePathLeaf (pathConfig.removePathLeaf thisFile))
	else
		@"C:\work\Ai\3dsmax-mcp"
	local repo = substituteString repoRoot "\\" "/"

	local py = "import sys\n"
	py += "sys.path.insert(0, r'" + repo + "')\n"
	py += "import importlib\n"
	py += "import maxmcp.ui.studio.launch as _launch\n"
	py += "importlib.reload(_launch)\n"
	py += "_launch.launch()\n"
	python.Execute py
)
