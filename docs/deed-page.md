# deed.page 网页发布

MiniAgent 可以通过可选的 `BaseTool` 集成使用 [deed.page](https://deed.page) 的
MCP 端点，把代理生成的 HTML 等静态文件发布为网页并拿到 URL。它通过 Streamable
HTTP 连接 `https://deed.page/mcp`，默认只注册 `deed_page_publish_site`，使用服务器
返回的参数 schema。原有的 Python、文件和 Bash 工具保持不变。

> **发布的页面是公开的**，任何拿到链接的人都能访问。不要发布密钥、凭据或私密内容。
> 建议传入 `confirm` 回调，在每次发布前由用户确认。

## 安装

此可选集成需要 **Python 3.10+**；MiniAgent 核心的 Python 要求不变。
在仓库根目录安装：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install '.[deedpage]'
```

deed.page 的 MCP 端点免费，无需 API key 或账号，不读取本地凭据。LLM 推理单独
计费，下面的代理示例仍需要 `OPENAI_API_KEY`。

## 在代理中启用

```python
import asyncio
import os

from mini_agent import MiniAgent, SimpleLLM
from mini_agent.deed_page import deed_page


def confirm(arguments):
    files = ", ".join(sorted(arguments.get("files", {})))
    return input(f"发布公开页面（{files}）？[y/N] ").strip().lower() == "y"


async def main():
    agent = MiniAgent(
        SimpleLLM(api_key=os.environ["OPENAI_API_KEY"], model="gpt-4o-mini"),
        system_prompt=(
            "你是写作助手。用户要求分享时，用 deed_page_publish_site 发布 HTML，"
            "files 形如 {\"index.html\": \"<!doctype html>...\"}，临时页面请设置 "
            "ttl_seconds。回答时附上页面 URL。"
        ),
    )
    async with deed_page(agent.tools, confirm=confirm):
        print(await agent.run("写一页介绍 asyncio TaskGroup 的 HTML，发布 1 小时"))


asyncio.run(main())
```

工具只在 `async with` 内可用；整个 `agent.run` 必须在该上下文内执行。退出时关闭
连接并恢复同名的已有工具。`confirm` 返回 False 时不会调用服务器，工具返回
`success=False`。

`publish_site` 的结果包含页面 `url`、`slug` 和管理用的 `token`。token 会出现在
工具输出中（模型也能看到），如需之后更新或删除页面请保存。传入
`allow_delete=True` 会额外注册 `deed_page_delete_site`。

## 直接调用（不需要 LLM key）

```python
import asyncio
import json

from mini_agent.deed_page import deed_page
from mini_agent.tools import ToolCollection


async def main():
    tools = ToolCollection()
    async with deed_page(tools, allow_delete=True):
        published = await tools.execute_tool(
            "deed_page_publish_site",
            files={"index.html": "<!doctype html><h1>Hello from MiniAgent</h1>"},
            ttl_seconds=600,
        )
        site = json.loads(published.output)
        print(site["url"])
        deleted = await tools.execute_tool(
            "deed_page_delete_site", slug=site["slug"], token=site["token"]
        )
        print(deleted.output if deleted.success else deleted.error)


asyncio.run(main())
```

服务器错误或调用异常通过 `success=False` 和 `error` 返回。连接或工具发现失败会
抛出异常；取消不会转换成普通工具错误。`timeout`（秒，默认 60）限制 HTTP 读取和
MCP 响应等待。限制和完整工具列表见 [deed.page 文档](https://deed.page/docs)。

## 测试

```bash
python -m pip install pytest pytest-asyncio
python -m pytest tests/test_deed_page.py
```

测试使用模拟 MCP 会话和脚本化 LLM，验证工具注册、代理调用、确认回调、错误及清理。
它们不调用付费 LLM，也不会发布真实页面。
