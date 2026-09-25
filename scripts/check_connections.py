"""Opt-in network smoke test; no credentials or raw responses are printed."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import Settings  # noqa: E402
from app.provider import FlashScore  # noqa: E402
from app.store import Store  # noqa: E402


def rpc_payload(response):
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data: "):
                try:
                    value = json.loads(line[6:])
                    if "result" in value or "error" in value:
                        return value
                except ValueError:
                    continue
        return {}
    return response.json()


async def main():
    settings = Settings.load()
    store = Store(settings.database)
    provider = FlashScore(settings, store)
    try:
        tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
        matches, cached, rejected = await provider.fixtures(tomorrow)
        store.save_matches(matches)
        print(f"REST: {len(matches)} fixtures; rejected={rejected}; cached={cached}")
        eligible = [m for m in matches if m.status == "scheduled" and m.home_id and m.away_id]
        preferred = [m for m in eligible if m.country in ("England", "Spain", "Romania")]
        if eligible:
            match = (preferred or eligible)[0]
            history, warnings = await provider.history(match)
            store.save_matches(history)
            from app.model import predict

            prediction = predict(match, store.matches())
            print(f"History: {len(history)} results; warnings={len(warnings)}")
            print(f"Model sample: {prediction['sample']}; quality={prediction['quality']}")
    finally:
        await provider.client.aclose()
    if "--mcp" in sys.argv:
        headers = {
            "x-api-host": "flashscore4.p.rapidapi.com",
            "x-api-key": settings.api_key,
            "accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            init = await client.post(
                "https://mcp.rapidapi.com",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "footypreds-check", "version": "7.0"},
                    },
                },
            )
            print(f"MCP initialize: HTTP {init.status_code}")
            if init.status_code == 200:
                result = rpc_payload(init)
                if "error" in result:
                    print("MCP returned an initialization error (details suppressed).")
                    return
                if init.headers.get("mcp-session-id"):
                    headers["mcp-session-id"] = init.headers["mcp-session-id"]
                headers["MCP-Protocol-Version"] = result.get("result", {}).get(
                    "protocolVersion", "2025-03-26"
                )
                await client.post(
                    "https://mcp.rapidapi.com",
                    headers=headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
                tools = await client.post(
                    "https://mcp.rapidapi.com",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
                print(f"MCP tools/list: HTTP {tools.status_code}")
                if tools.status_code == 200:
                    listing = rpc_payload(tools).get("result", {}).get("tools", [])
                    print(f"MCP available tools: {len(listing)}")


if __name__ == "__main__":
    asyncio.run(main())
