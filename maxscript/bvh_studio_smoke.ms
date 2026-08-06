-- bvh_studio_smoke.ms — Task 9 게이트
--
-- QWebEngineView 가 3ds Max 안에서 실제로 뜨는지 확인한다.
-- 결과는 <repo>\.smoke\report.json 과 view.png 에 남는다.
--
-- 실행: Scripting > Run Script 로 이 파일을 지정한다.
-- 원본 bvh_biped_ui.ms 와 공존하며, 그 파일을 건드리지 않는다.

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
	py += "import src.ui.studio.smoke as _smoke\n"
	py += "importlib.reload(_smoke)\n"
	py += "_smoke.run()\n"
	python.Execute py

	format "BVH Studio 스모크를 실행했습니다.\n"
	format "몇 초 뒤 결과가 여기에 쓰입니다: %\n" (repoRoot + "\\.smoke\\report.json")
)
