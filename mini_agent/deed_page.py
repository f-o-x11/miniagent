"""可选的 deed.page MCP 发布工具，使用 Streamable HTTP，无需 API key。"""

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator, Callable, Dict, Optional, Tuple

from pydantic import PrivateAttr

from mini_agent.tools import BaseTool, ToolCollection, ToolResult

DEEDPAGE_MCP_URL = "https://deed.page/mcp"
USER_AGENT = "miniagent/1.0.0 (deed.page MCP)"

# 发布前确认：收到工具参数，返回 True 才会真正发布。
ConfirmFn = Callable[[Dict[str, Any]], bool]


class DeedPageTool(BaseTool):
    """将服务器提供的工具 schema 和执行结果转换为 MiniAgent 格式。"""

    _session: Any = PrivateAttr()
    _remote_name: str = PrivateAttr()
    _confirm: Optional[ConfirmFn] = PrivateAttr(default=None)
    _active: bool = PrivateAttr(default=True)

    async def execute(self, **kwargs: Any) -> ToolResult:
        """调用 MCP 工具；保留文本、结构化输出及服务器错误。"""
        if not self._active:
            return ToolResult(success=False, error="deed.page MCP connection is closed")
        if self._confirm is not None and not self._confirm(dict(kwargs)):
            return ToolResult(success=False, error="publish declined by user")
        try:
            result = await self._session.call_tool(self._remote_name, dict(kwargs))
            text = "\n".join(
                block.text for block in result.content if block.type == "text"
            )
            if not text and result.structuredContent is not None:
                text = json.dumps(result.structuredContent, ensure_ascii=False)
            if result.isError:
                return ToolResult(success=False, error=text or "deed.page MCP error")
            return ToolResult(success=True, output=text)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))


@asynccontextmanager
async def deed_page(
    tools: ToolCollection,
    timeout: float = 60.0,
    allow_delete: bool = False,
    confirm: Optional[ConfirmFn] = None,
) -> AsyncIterator[None]:
    """在上下文内注册 ``deed_page_publish_site``，退出时关闭连接并恢复原工具。

    发布的页面是公开的。``confirm`` 可在每次发布前询问用户；``allow_delete``
    为 True 时额外注册 ``deed_page_delete_site``。需要 Python 3.10+ 和
    ``pip install '.[deedpage]'``。连接及发现失败会向调用者抛出异常；工具调用
    失败返回 ToolResult。取消操作继续向上传播。
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    wanted: Tuple[str, ...] = ("publish_site",)
    if allow_delete:
        wanted += ("delete_site",)
    registered: Dict[str, DeedPageTool] = {}
    previous: Dict[str, BaseTool] = {}
    async with streamablehttp_client(
        DEEDPAGE_MCP_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        sse_read_timeout=timeout,
    ) as (read, write, _):
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=timeout)
        ) as session:
            await session.initialize()
            discovered = await session.list_tools()
            available = {tool.name: tool for tool in discovered.tools}
            if not set(wanted).issubset(available):
                raise RuntimeError("deed.page MCP must provide " + ", ".join(wanted))
            try:
                for remote_name in wanted:
                    remote = available[remote_name]
                    tool = DeedPageTool(
                        name="deed_page_" + remote_name,
                        description=remote.description or remote_name,
                        parameters=remote.inputSchema,
                    )
                    tool._session = session
                    tool._remote_name = remote_name
                    if remote_name == "publish_site":
                        tool._confirm = confirm
                    if tool.name in tools.tools:
                        previous[tool.name] = tools.tools[tool.name]
                    registered[tool.name] = tool
                    tools.register_tool(tool)
                yield
            finally:
                for name, tool in registered.items():
                    tool._active = False
                    if tools.tools.get(name) is tool:
                        if name in previous:
                            tools.tools[name] = previous[name]
                        else:
                            del tools.tools[name]
