"""Long-term memory for an agent, through swsdb's MCP server: remember facts, recall them later by meaning.

    pip install "swsdb[mcp]"
    python memory_demo.py                              # http://localhost:6464/mcp/rw
    SWSDB_API_KEY=... python memory_demo.py --url https://db.example.com

This is what an assistant does when it calls the `remember` and `recall` tools; connect a real one with the configs
in this folder's README.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

FACTS = [
    ("The user prefers invoices in euros, sent on the first of the month.", ["billing"]),
    ("The user's team deploys on Tuesdays and freezes code on Mondays at noon.", ["work"]),
    ("The user is allergic to peanuts; never suggest restaurants without checking.", ["personal"]),
]


async def main(url: str, key: str | None) -> None:
    headers = {"api-key": key} if key else {}
    async with httpx.AsyncClient(headers=headers, timeout=180) as http, \
            streamable_http_client(f"{url.rstrip('/')}/mcp/rw", http_client=http) as (read, write, _), \
            ClientSession(read, write) as session:
        await session.initialize()
        tools = [t.name for t in (await session.list_tools()).tools]
        print("tools:", ", ".join(tools), "\n")
        for text, tags in FACTS:
            r = await session.call_tool("remember", {"text": text, "tags": tags})
            print("remembered:", r.content[0].text)
        for question in ("When should invoices go out?", "Is there any food I should avoid suggesting?"):
            r = await session.call_tool("recall", {"query": question, "k": 1})
            print(f"\nrecall {question!r}\n{r.content[0].text}")
        r = await session.call_tool("recall", {"query": "deploy schedule", "tags": ["billing"]})
        print(f"\nrecall 'deploy schedule' among billing memories only\n{r.content[0].text}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=os.environ.get("SWSDB_URL", "http://localhost:6464"))
    a = p.parse_args()
    asyncio.run(main(a.url, os.environ.get("SWSDB_API_KEY")))
