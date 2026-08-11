"""Consumer agent: the proof that a generated server is actually useful.

Deliberately minimal — no framework, no orchestration graph. The generated
MCP server is spawned over stdio, its tools are bridged into the Anthropic
SDK's tool runner, and the model works the task in a plain agentic loop. If
the pipeline did its job, all of the intelligence this file needs is already
in the generated tool descriptions; a thin agent is the point being made.
"""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from toolsmith.llm import MODEL

SYSTEM = """\
You are a capable personal assistant. Use the available tools to answer with
real data — never guess values a tool can fetch. Today is {today}.
Answer concisely and lead with the practical takeaway.
"""


async def run_agent(server_path: Path, task: str) -> str:
    """Run one task against a generated server; prints the tool-call trace and
    returns the agent's final answer."""
    params = StdioServerParameters(
        command=sys.executable, args=[str(server_path), "stdio"], env=dict(os.environ)
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as mcp:
        await mcp.initialize()
        served = (await mcp.list_tools()).tools
        print(f"    agent has {len(served)} tools: {', '.join(t.name for t in served)}")

        runner = AsyncAnthropic().beta.messages.tool_runner(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM.format(
                today=datetime.now(UTC).astimezone().strftime("%A, %B %d, %Y")
            ),
            messages=[{"role": "user", "content": task}],
            tools=[async_mcp_tool(tool, mcp) for tool in served],
        )

        final_text = ""
        async for message in runner:
            for block in message.content:
                if block.type == "tool_use":
                    print(f"    → {block.name}({_compact(block.input)})")
                elif block.type == "text" and block.text.strip():
                    final_text = block.text
        return final_text


def _compact(payload: dict, limit: int = 120) -> str:
    text = ", ".join(f"{key}={value!r}" for key, value in payload.items())
    return text if len(text) <= limit else text[: limit - 3] + "..."
