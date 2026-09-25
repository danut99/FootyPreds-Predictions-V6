"""Stdio bridge for an MCP client; credentials are passed through environment variables."""

import os
import shutil
import subprocess
import sys

from footypreds.config import ROOT, Settings


def main():
    settings = Settings.load()
    if not settings.api_key:
        print("Configurează RAPIDAPI_KEY în .env înainte de pornirea MCP.", file=sys.stderr)
        return 1
    npx = shutil.which("npx.cmd" if os.name == "nt" else "npx")
    if not npx:
        print("MCP necesită Node.js și npx.", file=sys.stderr)
        return 1
    environment = os.environ.copy()
    environment["RAPIDAPI_KEY"] = settings.api_key
    command = [
        npx,
        "-y",
        "mcp-remote@0.14.3",
        "https://mcp.rapidapi.com",
        "--header",
        "x-api-host:flashscore4.p.rapidapi.com",
        "--header",
        "x-api-key:${RAPIDAPI_KEY}",
    ]
    return subprocess.call(command, cwd=ROOT, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
