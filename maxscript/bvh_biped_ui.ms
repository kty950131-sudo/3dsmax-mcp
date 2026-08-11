-- bvh_biped_ui.ms — BVH → Biped import UI (kimodo/SMPL-X friendly)
--
-- Run via Scripting > Run Script (or drop on a toolbar as a macroscript).
-- Conversion reuses maxmcp/helpers/bvh.py through Max's embedded Python, so the
-- rewrite rules (strip dummy root, prune extras, rename to Character Studio
-- vocabulary) live in one place.
--
-- Mixamo-style extras:
--   * 배치 간격  — each import is placed at a cumulative X offset ("Placement" layer)
--   * 팔 간격    — arm-space slider, uniform outward upper-arm rotation ("ArmSpace" layer)

global BvhBipedUi
global bvhUiOut, bvhUiUpright, bvhUiErr, bvhUiSeconds
global bvhUiSyncOut, bvhUiSyncErr

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
	fn convertClip srcPath xOffset:0 speed:1.0 trimStart:0.0 trimEnd:1.0 =
	(
		bvhUiOut = ""
		bvhUiErr = ""
		bvhUiUpright = true
		bvhUiSeconds = 0.0
		local repo = substituteString repoRoot "\\" "/"
		local src = substituteString srcPath "\\" "/"
		local py = "import sys\n"
		py += "from pymxs import runtime as rt\n"
		py += "sys.path.insert(0, r'" + repo + "')\n"
		py += "try:\n"
		py += "    from pathlib import Path\n"
		py += "    import importlib\n"
		py += "    import maxmcp.helpers.bvh as _bvh\n"
		py += "    importlib.reload(_bvh)\n"
		py += "    from maxmcp.helpers.bvh import prepare_for_biped, has_upright_spine, DEFAULT_BIPED_PRUNE\n"
		py += "    src = Path(r'" + src + "')\n"
		py += "    text = src.read_text(encoding='utf-8')\n"
		py += "    out = src.with_name(src.stem + '_biped.bvh')\n"
		py += "    converted = prepare_for_biped(text, prune=DEFAULT_BIPED_PRUNE, offset=(" + (xOffset as string) + ", 0.0, 0.0), speed=" + (speed as string) + ", trim_range=(" + (trimStart as string) + ", " + (trimEnd as string) + "))\n"
		py += "    out.write_text(converted, encoding='utf-8', newline='\\n')\n"
		py += "    rt.bvhUiOut = str(out)\n"
		py += "    rt.bvhUiUpright = has_upright_spine(text)\n"
		py += "    _done = _bvh.parse_bvh(converted)\n"
		py += "    rt.bvhUiSeconds = len(_done.frames) * _done.frame_time\n"
		py += "except Exception as exc:\n"
		py += "    rt.bvhUiErr = str(exc)\n"
		python.Execute py
		if bvhUiErr != "" then undefined else bvhUiOut
	),

	-- Pull new/changed .bvh clips from the artoke GitHub repo into folder
	-- (maxmcp/helpers/github_sync.py via gh CLI). Returns a status string.
	fn syncFromGithub folder =
	(
		bvhUiSyncOut = ""
		bvhUiSyncErr = ""
		local repo = substituteString repoRoot "\\" "/"
		local dst = substituteString folder "\\" "/"
		local py = "import sys\n"
		py += "from pymxs import runtime as rt\n"
		py += "sys.path.insert(0, r'" + repo + "')\n"
		py += "try:\n"
		py += "    import importlib\n"
		py += "    import maxmcp.helpers.github_sync as _gs\n"
		py += "    importlib.reload(_gs)\n"
		py += "    res = _gs.sync_motions(r'" + dst + "')\n"
		py += "    rt.bvhUiSyncOut = ', '.join(res['downloaded'])\n"
		py += "except Exception as exc:\n"
		py += "    rt.bvhUiSyncErr = str(exc)\n"
		python.Execute py
		if bvhUiSyncErr != "" then "ERROR: " + bvhUiSyncErr
		else if bvhUiSyncOut == "" then "최신 상태 — 새 모션 없음"
		else "다운로드: " + bvhUiSyncOut
	),

	-- Scene bipeds = nodes whose transform controller is the biped COM.
	fn sceneBipeds =
	(
		for o in objects where (classOf o.controller == Vertical_Horizontal_Turn) collect o
	),

	fn importClip srcPath bipName doConvert xOffset speed:1.0 trimStart:0.0 trimEnd:1.0 doMirror:false =
	(
		if not doesFileExist srcPath do return "ERROR: file not found: " + srcPath
		local loadPath = srcPath
		local upright = true
		if doConvert do
		(
			loadPath = convertClip srcPath xOffset:xOffset speed:speed trimStart:trimStart trimEnd:trimEnd
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
		if doMirror do ( try ( biped.mirror c ) catch () )
		importCount += 1
		local msg = "OK: " + bip.name
		if doConvert and bvhUiSeconds > 0 then
			msg += " ~" + (((bvhUiSeconds * frameRate) as integer) as string) + "f"
		else
			msg += " " + (animationRange as string)
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
	),

	-- Save the biped's current motion as a .bip clip and stage it in the
	-- Motion Mixer, where speed/trim/loop/blending stay editable. Uses only
	-- calls verified safe on Max 2026 — MXClip introspection (getClip)
	-- hard-crashes, so clips are arranged in the Mixer window itself.
	fn sendToMixer bip =
	(
		local c = bip.transform.controller
		if classOf c != Vertical_Horizontal_Turn do return "ERROR: not a biped root: " + bip.name
		if c.figureMode do return "ERROR: 피겨 모드를 먼저 해제하세요: " + bip.name
		local dir = (defaultFolder()) + "\\clips"
		makeDir dir all:true
		local bipFile = dir + "\\" + bip.name + ".bip"
		-- saveBipFile refuses while a biped mode (e.g. mixer) is active
		if c.mixerMode do c.mixerMode = false
		local saved = false
		try ( saved = biped.saveBipFile c bipFile ) catch ()
		if not saved do return "ERROR: saveBipFile failed: " + bipFile
		c.mixerMode = true
		local m = c.mixer
		if m.numTrackgroups == 0 do appendTrackgroup m
		local tr = getTrack (getTrackgroup m 1) 1
		local ok = false
		try ( ok = appendClip tr bipFile false 0f ) catch ()
		if not ok do return "ERROR: appendClip failed: " + bipFile
		theMixer.updateDisplay()
		theMixer.showMixer()
		local msg = "OK: " + bip.name + " → Mixer"
		if tr.numClips > 1 do msg += " (클립 " + (tr.numClips as string) + "개 — 믹서 창에서 배치 조정)"
		msg
	)
)

rollout roBvhBiped "BVH → Biped (kimodo)"
(
	edittext etFolder "폴더:" fieldWidth:220 across:2
	button btBrowse "..." width:24 align:#right
	button btRefresh "새로고침" width:130 across:2
	button btSync "GitHub에서 가져오기" width:130 align:#right
	listbox lbClips "클립:" height:14
	edittext etName "이름:" fieldWidth:200
	checkbox cbConvert "kimodo 변환 (루트 제거·이름 매핑)" checked:true
	spinner spSpacing "배치 간격 X:" range:[0, 2000, 120] type:#integer fieldWidth:60

	group "클립 조정 (Mixamo식)"
	(
		spinner spSpeed "속도 %:" range:[25, 400, 100] type:#integer fieldWidth:50
		spinner spTrimStart "시작 %:" range:[0, 99, 0] type:#integer fieldWidth:50 across:2
		spinner spTrimEnd "끝 %:" range:[1, 100, 100] type:#integer fieldWidth:50
		checkbox cbMirror "미러 (좌우 반전)"
	)

	button btImport "바이패드 생성 + 임포트" width:264 height:30
	label lbStatus "" align:#left

	group "임포트 후 조절"
	(
		dropdownlist ddBiped "바이패드:"
		spinner spArm "팔 간격(°):" range:[0, 45, 10] type:#integer fieldWidth:50 across:2
		button btArm "적용" width:80 align:#right
		button btMixer "Motion Mixer로 보내기" width:240
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
	on btSync pressed do
	(
		lbStatus.text = "GitHub 동기화 중..."
		lbStatus.text = BvhBipedUi.syncFromGithub etFolder.text
		refreshClips()
	)
	on lbClips selected idx do
	(
		if idx > 0 do etName.text = "Bip_" + lbClips.items[idx]
	)
	on btImport pressed do
	(
		if lbClips.selection == 0 then lbStatus.text = "클립을 선택하세요"
		else if spTrimStart.value >= spTrimEnd.value then lbStatus.text = "트림 범위가 잘못됨 (시작 < 끝)"
		else
		(
			local src = etFolder.text + "\\" + lbClips.items[lbClips.selection] + ".bvh"
			local xoff = BvhBipedUi.importCount * spSpacing.value
			lbStatus.text = BvhBipedUi.importClip src etName.text cbConvert.checked xoff \
				speed:(spSpeed.value / 100.0) \
				trimStart:(spTrimStart.value / 100.0) \
				trimEnd:(spTrimEnd.value / 100.0) \
				doMirror:cbMirror.checked
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
	on btMixer pressed do
	(
		if ddBiped.selection == 0 then lbStatus.text = "바이패드를 선택하세요"
		else
		(
			local bip = getNodeByName ddBiped.items[ddBiped.selection]
			if bip == undefined then lbStatus.text = "ERROR: 바이패드를 찾을 수 없음"
			else lbStatus.text = BvhBipedUi.sendToMixer bip
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
	createDialog roBvhBiped 300 650
)
