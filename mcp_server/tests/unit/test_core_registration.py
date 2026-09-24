import pytest


@pytest.mark.asyncio
async def test_observation_tools_registered(monkeypatch):
    monkeypatch.setenv("JHE_BASE_URL", "http://localhost:8400")
    monkeypatch.setenv("JHE_CLIENT_ID", "test-client")
    monkeypatch.setenv("MCP_RESOURCE_URL", "http://localhost:8401/")
    from jhe_mcp.config import Settings
    from jhe_mcp.core import build_server

    mcp = build_server(Settings.from_env())
    # Server instructions are sent to every client on initialize.
    assert mcp.instructions and "get_patient_date_range" in mcp.instructions
    assert "search_patients" in mcp.instructions
    assert "get_server_capabilities" in mcp.instructions
    names = {tool.name for tool in await mcp.list_tools()}
    assert {
        "get_patient_observations",
        "count_patient_observations",
        "count_study_observations",
        "summarize_patient_observations",
        "get_patient_date_range",
        "search_patients",
        "get_server_capabilities",
    } <= names


@pytest.mark.asyncio
async def test_tool_failure_logs_traceback_without_message(monkeypatch, caplog):
    monkeypatch.setenv("JHE_BASE_URL", "http://localhost:8400")
    monkeypatch.setenv("JHE_CLIENT_ID", "test-client")
    monkeypatch.setenv("MCP_RESOURCE_URL", "http://localhost:8401/")
    from jhe_mcp.config import Settings
    from jhe_mcp.core import build_server
    from mcp.server.fastmcp.exceptions import ToolError

    secret = "Pat " + "Jones"

    async def boom(**_kwargs):
        raise TypeError(secret)

    monkeypatch.setattr("jhe_mcp.tools.study.get_study_count", boom)
    mcp = build_server(Settings.from_env())
    with pytest.raises(ToolError):
        await mcp.call_tool("get_study_count", {})
    record = next(r for r in caplog.records if r.name == "jhe_mcp.core")
    assert "get_study_count" in record.getMessage()
    assert "TypeError" in record.getMessage()
    assert "boom" in record.getMessage()
    # Exception messages can echo JHE response bodies, so they stay out of the log.
    assert "Pat Jones" not in caplog.text
