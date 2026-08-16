"""Consumer agent: the proof that a generated server is actually useful.

Deliberately thin — no orchestration graph, no planning scaffold. The
generated MCP server is spawned over stdio and handed to the model as a
toolset, and the model works the task in a plain agentic loop. If the
pipeline did its job, all the intelligence this file needs is already in the
generated tool descriptions; a thin agent is the point being made.
"""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset, StdioTransport
from pydantic_ai.messages import FunctionToolCallEvent
from pydantic_ai.models.bedrock import BedrockModelSettings
from pydantic_ai.settings import ModelSettings

from handyman.llm import model_name

SYSTEM = """\
You are a capable personal assistant. Use the available tools to answer with
real data — never guess values a tool can fetch. Today is {today}.
Answer concisely and lead with the practical takeaway.
"""


async def run_agent(server_path: Path, task: str) -> str:
    """Run one task against a generated server; prints the tool-call trace and
    returns the agent's final answer."""
    server = MCPToolset(
        StdioTransport(
            command=sys.executable, args=[str(server_path), "stdio"], env=dict(os.environ)
        )
    )
    agent = Agent(
        model_name(),
        system_prompt=SYSTEM.format(
            today=datetime.now(UTC).astimezone().strftime("%A, %B %d, %Y")
        ),
        toolsets=[server],
        model_settings=_cache_settings(),
    )
    async with agent:
        result = await agent.run(task, event_stream_handler=_trace)
    usage = result.usage
    print(
        f"    usage: {usage.input_tokens:,} in ({usage.cache_read_tokens:,} cached), "
        f"{usage.output_tokens:,} out"
    )
    return result.output


def _cache_settings() -> ModelSettings | None:
    """Prompt caching for the agent loop, which re-sends the tool schema, the
    system prompt, and the whole conversation so far on every turn — cache
    reads bill at ~10% of fresh input, roughly a 5x saving on fan-out-heavy
    tasks. Bedrock-only; other providers run with their defaults."""
    if not model_name().startswith("bedrock:"):
        return None
    return BedrockModelSettings(
        bedrock_cache_tool_definitions=True,
        bedrock_cache_instructions=True,
        bedrock_cache_messages=True,
    )


async def _trace(_ctx, events) -> None:
    """Print each tool call as the model makes it — the demo's visible pulse."""
    async for event in events:
        if isinstance(event, FunctionToolCallEvent):
            print(f"    → {event.part.tool_name}({_compact(event.part.args)})")


def _compact(args: object, limit: int = 120) -> str:
    if isinstance(args, dict):
        text = ", ".join(f"{key}={value!r}" for key, value in args.items())
    else:
        text = str(args or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."
