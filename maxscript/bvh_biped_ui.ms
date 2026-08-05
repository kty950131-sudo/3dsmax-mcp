-- bvh_biped_ui.ms — BVH → Biped import UI (kimodo/SMPL-X friendly)
--
-- Run via Scripting > Run Script (or drop on a toolbar as a macroscript).
-- Conversion reuses src/helpers/bvh.py through Max's embedded Python, so the
-- rewrite rules (strip dummy root, prune extras, rename to Character Studio
-- vocabulary) live in one place.
--
-- Mixamo-style extras:
--   * 배치 간격  — each import is placed at a cumulative X offset ("Placement" layer)
--   * 팔 간격    — arm-space slider, uniform outward upper-arm rotation ("ArmSpace" layer)

global BvhBipedUi
global bvhUiOut, bvhUiUpright, bvhUiErr

struct BvhBipedUiStruct
(
	repoRoot = "",
	iniFile = ((getDir #userScripts) + "\\bvh_biped_ui.ini"),
	importCount = 0,

	fn defaultFolder =
	(
		local saved = getINISetting iniFile "bvhui" "folder"
		if saved != "" and doesFileExist saved then saved else @"C:\work\Ai\bvh-library"
	),

	fn saveFolder folder =
	(
		setINISetting iniFile "bvhui" "folder" folder
	),

	fn listClips folder =
	(
		local files = getFiles (folder + "\\*.bvh")
		local clips = #()
		for f in files do
		(
			local stem = getFilenameFile f
			if not (matchPattern stem pattern:"*_biped") do append clips stem
		)
		sort clips
		clips
	),

	-- Convert srcPath for biped via the Python helper, baking an X placement
	-- offset into the root position channels. Returns the path to load, or
	-- undefined on error (bvhUiErr set).
	fn convertClip srcPath xOffset:0 =
	(
		bvhUiOut = ""
		bvhUiErr = ""
		bvhUiUpright = true
		local repo = substituteString repoRoot "\\" "/"
		local src = substituteString srcPath "\\" "/"
		local py = "import sys\n"
		py += "from pymxs import runtime as rt\n"
		py += "sys.path.insert(0, r'" + repo + "')\n"
		py += "try:\n"
		py += "    from pathlib import Path\n"
		py += "    import importlib\n"
		py += "    import src.helpers.bvh as _bvh\n"
		py += "    importlib.reload(_bvh)\n"
		py += "    from src.helpers.bvh import prepare_for_biped, has_upright_spine, DEFAULT_BIPED_PRUNE\n"
		py += "    src = Path(r'" + src + "')\n"
		py += "    text = src.read_text(encoding='utf-8')\n"
		py += "    out = src.with_name(src.stem + '_biped.bvh')\n"
		py += "    converted = prepare_for_biped(text, prune=DEFAULT_BIPED_PRUNE, offset=(" + (xOffset as string) + ", 0.0, 0.0))\n"
		py += "    out.write_text(converted, encoding='utf-8', newline='\\n')\n"
		py += "    rt.bvhUiOut = str(out)\n"
		py += "    rt.bvhUiUpright = has_upright_spine(text)\n"
		py += "except Exception as exc:\n"
		py += "    rt.bvhUiErr = str(exc)\n"
		python.Execute py
		if bvhUiErr != "" then undefined else bvhUiOut
	),

	-- Scene bipeds = nodes whose transform controller is the biped COM.
	fn sceneBipeds =
	(
		for o in objects where (classOf o.controller == Vertical_Horizontal_Turn) collect o
	),

	fn importClip srcPath bipName doConvert xOffset =
	(
		if not doesFileExist srcPath do return "ERROR: file not found: " + srcPath
		local loadPath = srcPath
		local upright = true
		if doConvert do
		(
			loadPath = convertClip srcPath xOffset:xOffset
			if loadPath == undefined do return "ERROR: convert failed: " + bvhUiErr
			upright = bvhUiUpright
		)
		local bip = biped.createNew 170 -90 [0, 0, 0]
		if bip == undefined do return "ERROR: biped.createNew failed"
		if bipName != "" do bip.name = bipName
		local c = bip.transform.controller
		local ok = false
		local oldQ = setQuietMode true
		try ( ok = biped.loadMocapFile c loadPath ) catch ()
		setQuietMode oldQ
		if not ok do
		(
			try ( delete bip ) catch ()
			return "ERROR: loadMocapFile rejected " + loadPath
		)
		importCount += 1
		local msg = "OK: " + bip.name + " " + (animationRange as string)
		if not upright do msg += " | 경고: T포즈 골격이 아님(_tpose 파일 권장) — 자세가 틀어질 수 있음"
		msg
	),

	-- Mixamo-style arm-space: outward upper-arm rotation on an "ArmSpace"
	-- layer, keyed at frame 0 so the offset holds across the whole clip.
	fn applyArmSpace bip degrees =
	(
		local c = bip.transform.controller
		if classOf c != Vertical_Horizontal_Turn do return "ERROR: not a biped root: " + bip.name
		for i = (biped.numLayers c) to 1 by -1 do
			if (biped.getLayerName c i) == "ArmSpace" do biped.deleteLayer c i
		if degrees != 0 do
		(
			biped.createLayer c ((biped.numLayers c) + 1) "ArmSpace"
			biped.setCurrentLayer c (biped.numLayers c)
			local lua = biped.getNode c #larm link:2
			local rua = biped.getNode c #rarm link:2
			sliderTime = 0f
			animate on
			(
				at time 0
				(
					in coordsys local rotate lua (angleaxis degrees [0, 0, 1])
					in coordsys local rotate rua (angleaxis (-degrees) [0, 0, 1])
					biped.addNewKey lua.controller 0
					biped.addNewKey rua.controller 0
				)
			)
			-- keep the ArmSpace layer current: the biped composites layers up
			-- to the current one, so switching back to 0 would hide the offset
		)
		"OK: " + bip.name + " 팔 간격 " + (degrees as string) + "°"
	)
)

rollout roBvhBiped "BVH → Biped (kimodo)"
(
	edittext etFolder "폴더:" fieldWidth:220 across:2
	button btBrowse "..." width:24 align:#right
	button btRefresh "새로고침" width:264
	listbox lbClips "클립:" height:14
	edittext etName "이름:" fieldWidth:200
	checkbox cbConvert "kimodo 변환 (루트 제거·이름 매핑)" checked:true
	spinner spSpacing "배치 간격 X:" range:[0, 2000, 120] type:#integer fieldWidth:60
	button btImport "바이패드 생성 + 임포트" width:264 height:30
	label lbStatus "" align:#left

	group "팔 간격 (Arm-Space)"
	(
		dropdownlist ddBiped "바이패드:"
		spinner spArm "각도(°):" range:[0, 45, 10] type:#integer fieldWidth:50 across:2
		button btArm "적용" width:80 align:#right
	)

	fn refreshClips =
	(
		lbClips.items = BvhBipedUi.listClips etFolder.text
	)

	fn refreshBipeds =
	(
		ddBiped.items = for o in BvhBipedUi.sceneBipeds() collect o.name
	)

	on roBvhBiped open do
	(
		etFolder.text = BvhBipedUi.defaultFolder()
		refreshClips()
		refreshBipeds()
	)
	on btBrowse pressed do
	(
		local p = getSavePath caption:"BVH 폴더 선택" initialDir:etFolder.text
		if p != undefined do
		(
			etFolder.text = p
			BvhBipedUi.saveFolder p
			refreshClips()
		)
	)
	on btRefresh pressed do
	(
		refreshClips()
		refreshBipeds()
	)
	on lbClips selected idx do
	(
		if idx > 0 do etName.text = "Bip_" + lbClips.items[idx]
	)
	on btImport pressed do
	(
		if lbClips.selection == 0 then lbStatus.text = "클립을 선택하세요"
		else
		(
			local src = etFolder.text + "\\" + lbClips.items[lbClips.selection] + ".bvh"
			local xoff = BvhBipedUi.importCount * spSpacing.value
			lbStatus.text = BvhBipedUi.importClip src etName.text cbConvert.checked xoff
			refreshBipeds()
		)
	)
	on btArm pressed do
	(
		if ddBiped.selection == 0 then lbStatus.text = "바이패드를 선택하세요"
		else
		(
			local bip = getNodeByName ddBiped.items[ddBiped.selection]
			if bip == undefined then lbStatus.text = "ERROR: 바이패드를 찾을 수 없음"
			else lbStatus.text = BvhBipedUi.applyArmSpace bip spArm.value
		)
	)
)

(
	BvhBipedUi = BvhBipedUiStruct()
	local thisFile = getThisScriptFilename()
	if thisFile != undefined then
		BvhBipedUi.repoRoot = pathConfig.removePathLeaf (pathConfig.removePathLeaf thisFile)
	else
		BvhBipedUi.repoRoot = @"C:\work\Ai\3dsmax-mcp"
	try ( destroyDialog roBvhBiped ) catch ()
	createDialog roBvhBiped 300 480
)
