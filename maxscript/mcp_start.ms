-- mcp_start.ms — MCP TCP 리스너(8765) 켜기
--
-- `mcp_server.ms` 는 헬퍼만 올리고 리스너를 켜지는 않는다("MCP Start" 매크로나
-- `MCP_Server.start()` 를 따로 불러야 한다). 이 파일을 뷰포트에 드래그하면
-- 헬퍼가 없으면 먼저 읽고, 그 다음 리스너를 켠다.
--
-- 정상 경로는 네임드 파이프 브릿지(\\.\pipe\3dsmax-mcp)다. 그것이 살아 있으면
-- TCP 는 필요 없으므로 켜지 않고 그렇게 알린다.

(
	local here = getFilenamePath (getThisScriptFilename())

	if MCP_Server == undefined then
	(
		local helpers = here + "mcp_server.ms"
		if doesFileExist helpers then fileIn helpers
		else format "MCP: mcp_server.ms 를 찾지 못했습니다: %\n" helpers
	)

	if MCP_Server == undefined then
	(
		format "MCP: 헬퍼를 올리지 못했습니다.\n"
	)
	else if MCP_Server.nativeBridgeAvailable() then
	(
		format "MCP: 네임드 파이프 브릿지가 이미 돌고 있습니다 — TCP 는 필요 없습니다.\n"
	)
	else if MCP_Server.isRunning then
	(
		format "MCP: TCP 리스너가 이미 켜져 있습니다 (포트 %).\n" MCP_Server.port
	)
	else
	(
		MCP_Server.start()
		format "MCP: TCP 리스너 시작 — 포트 %, 켜짐 = %\n" MCP_Server.port MCP_Server.isRunning
	)
)
