"""Cheap deterministic checks on a generated server.

Template-based generation makes most static defects impossible by
construction; these checks exist to keep that claim honest rather than
asserted. `boot_check` doubles as the source of truth for served tool names,
which the eval stage needs (arcade-mcp namespaces tools, e.g. `get_forecast`
on server `nws` is served as `Nws_GetForecast`).
"""

import asyncio
import os
import py_compile
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def static_check(server_path: Path) -> list[str]:
    """Return problems (empty list = pass): compile errors, then ruff on
    correctness-only rules. Style rules are deliberately excluded — style is
    the template's job, and linting generated code for taste is noise."""
    problems = []
    try:
        py_compile.compile(str(server_path), doraise=True)
    except py_compile.PyCompileError as error:
        return [f"does not compile: {error.msg}"]

    ruff = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "E9,F", "--no-cache", "--quiet",
         str(server_path)],
        capture_output=True,
        text=True,
        check=False,  # a nonzero exit is the finding, not an error
    )
    if ruff.returncode != 0:
        problems.append(f"correctness lint failed:\n{ruff.stdout.strip()}")
    return problems


def boot_check(server_path: Path) -> list[str]:
    """Boot the server over stdio and return the tool names it actually serves.

    Raises if the server cannot start (its stderr streams through to the
    console) — a boot failure is a generation bug, never something to paper
    over.
    """

    async def _list() -> list[str]:
        params = StdioServerParameters(
            command=sys.executable, args=[str(server_path), "stdio"], env=dict(os.environ)
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return [tool.name for tool in (await session.list_tools()).tools]

    return asyncio.run(_list())
