"""验证可选 deed.page MCP 工具与 MiniAgent 的原生调用路径。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")
from mcp.types import CallToolResult, TextContent, Tool

from mini_agent import MiniAgent
from mini_agent.deed_page import DEEDPAGE_MCP_URL, USER_AGENT, deed_page
from mini_agent.llm import LLMResponse
from mini_agent.tools import PythonExecutor, ToolCollection

PUBLISHED = '{"url": "https://quiet-field-a1b2.deed.page/", "slug": "quiet-field-a1b2"}'


@pytest.fixture
def server(monkeypatch):
    """记录传输参数和真实 SDK 数据类型；不发布真实页面。"""
    state = SimpleNamespace(calls=[], closed=False, error=None, result=None)
    schema = {"type": "object", "properties": {"files": {"type": "object"}}}
    state.tools = [
        Tool(name=name, description="Remote " + name, inputSchema=schema)
        for name in ("publish_site", "get_site", "delete_site")
    ]

    @asynccontextmanager
    async def transport(url, **kwargs):
        state.url = url
        state.transport = kwargs
        try:
            yield ("read", "write", None)
        finally:
            state.closed = True

    class Session:
        def __init__(self, read, write, **kwargs):
            assert (read, write) == ("read", "write")
            state.session = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            state.initialized = True

        async def list_tools(self):
            assert state.initialized
            return SimpleNamespace(tools=state.tools)

        async def call_tool(self, name, arguments):
            state.calls.append((name, arguments))
            if state.error:
                raise state.error
            return state.result or CallToolResult(
                content=[TextContent(type="text", text=PUBLISHED)]
            )

    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", transport)
    monkeypatch.setattr("mcp.ClientSession", Session)
    return state


async def test_agent_publishes_and_sees_url(server):
    """模型选择 -> agent.act -> ToolCollection -> MCP -> 模型收到页面 URL。"""

    class ScriptedLLM:
        async def chat(self, messages, system_prompt=None, tools=None):
            names = {t["function"]["name"] for t in tools}
            assert "deed_page_publish_site" in names
            assert "deed_page_delete_site" not in names
            if not any(message["role"] == "tool" for message in messages):
                return LLMResponse(
                    content="发布页面",
                    tool_calls=[
                        {
                            "id": "publish-1",
                            "function": {
                                "name": "deed_page_publish_site",
                                "arguments": '{"files":{"index.html":"<h1>Hi</h1>"},'
                                '"ttl_seconds":600}',
                            },
                        }
                    ],
                )
            assert "quiet-field-a1b2.deed.page" in messages[-1]["content"]
            return LLMResponse(content="https://quiet-field-a1b2.deed.page/")

    agent = MiniAgent(ScriptedLLM())
    defaults = dict(agent.tools.tools)
    async with deed_page(agent.tools, timeout=12):
        summary = await agent.run("Publish a page")
        assert "quiet-field-a1b2" in summary
    assert server.calls == [
        ("publish_site", {"files": {"index.html": "<h1>Hi</h1>"}, "ttl_seconds": 600})
    ]
    assert agent.tools.tools == defaults
    assert server.closed
    assert server.url == DEEDPAGE_MCP_URL
    assert server.transport == {
        "headers": {"User-Agent": USER_AGENT},
        "timeout": 12,
        "sse_read_timeout": 12,
    }
    assert server.session["read_timeout_seconds"] == timedelta(seconds=12)


async def test_confirm_declines_without_calling_server(server):
    tools = ToolCollection()
    seen = []

    def confirm(arguments):
        seen.append(arguments)
        return False

    async with deed_page(tools, confirm=confirm):
        result = await tools.execute_tool(
            "deed_page_publish_site", files={"index.html": "x"}
        )
    assert not result.success and result.error == "publish declined by user"
    assert seen == [{"files": {"index.html": "x"}}]
    assert server.calls == []


async def test_allow_delete_registers_delete_without_confirm(server):
    tools = ToolCollection()
    async with deed_page(tools, allow_delete=True, confirm=lambda _: False):
        result = await tools.execute_tool(
            "deed_page_delete_site", slug="quiet-field-a1b2", token="t"
        )
        assert result.success
        assert "deed_page_get_site" not in tools.tools
    assert server.calls == [("delete_site", {"slug": "quiet-field-a1b2", "token": "t"})]
    assert "deed_page_delete_site" not in tools.tools


async def test_errors_structured_output_and_cancellation(server):
    tools = ToolCollection()
    async with deed_page(tools):
        server.result = CallToolResult(content=[], structuredContent={"ok": True})
        result = await tools.execute_tool("deed_page_publish_site", files={})
        assert result.success and '"ok"' in result.output
        server.result = CallToolResult(
            content=[TextContent(type="text", text="site too large")], isError=True
        )
        result = await tools.execute_tool("deed_page_publish_site", files={})
        assert not result.success and result.error == "site too large"
        server.error = TimeoutError("timed out")
        result = await tools.execute_tool("deed_page_publish_site", files={})
        assert not result.success and result.error == "timed out"
        server.error = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await tools.execute_tool("deed_page_publish_site", files={})
    assert server.closed


async def test_restore_existing_tool_and_closed_tool(server):
    tools = ToolCollection()
    original = PythonExecutor(name="deed_page_publish_site")
    tools.register_tool(original)
    with pytest.raises(ValueError, match="body failed"):
        async with deed_page(tools):
            saved = tools.tools["deed_page_publish_site"]
            raise ValueError("body failed")
    assert tools.tools["deed_page_publish_site"] is original
    assert not (await saved.execute()).success


async def test_discovery_failure_and_invalid_timeout(server):
    tools = ToolCollection()
    defaults = dict(tools.tools)
    server.tools = []
    with pytest.raises(RuntimeError, match="must provide"):
        async with deed_page(tools):
            pytest.fail("missing tools must fail before registration")
    assert tools.tools == defaults
    with pytest.raises(ValueError, match="positive"):
        async with deed_page(ToolCollection(), timeout=0):
            pytest.fail("zero timeout must be rejected")
