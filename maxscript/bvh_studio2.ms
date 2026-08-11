-- bvh_studio2.ms — BVH Studio + RTMW3D 영상→Biped BVH 실행
--
-- 이 파일을 뷰포트에 드래그하거나 Scripting > Run Script 로 실행한다.
-- Max 2026 전용 (QtWebEngine). 2024 는 기존 bvh_biped_ui.ms 를 쓴다.
-- 상단 "영상 추가"에서 로컬 RTMW3D 관절 추정 작업을 시작할 수 있다.
-- 원본 bvh_biped_ui.ms 와 공존하며 로직은 전부 maxmcp/ 파이썬에 있다.

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
