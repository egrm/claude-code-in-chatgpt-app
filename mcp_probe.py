"""Real stdio MCP discovery against this checkout's server.py: initialize and list tools only, no inference.

May start the on-demand worker through its registered launcher. Run with the venv interpreter.
"""
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


async def main():
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'server.py')], cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = sorted(t.name for t in (await session.list_tools()).tools)
    print(json.dumps({'protocol': init.protocol_version, 'tools': tools}))


if __name__ == '__main__':
    from windows_process import hide_background_children
    hide_background_children()
    asyncio.run(asyncio.wait_for(main(), 60))
