#!/usr/bin/env python3
"""Wire Woxpas into Claude Desktop for the Docker-only install (no uv needed).

Standalone, stdlib-only, works on any Python 3. It writes a `woxpas` MCP server
that Claude Desktop launches via `docker compose run`, so the server runs in a
container and no secrets are written into the Claude config (they flow from
.env through the compose `mcp` service).

Run from the repo root:  python3 scripts/install_claude_desktop.py
                         python3 scripts/install_claude_desktop.py --print
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def claude_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Claude" / "claude_desktop_config.json"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "Claude" / "claude_desktop_config.json"


def main() -> int:
    ap = argparse.ArgumentParser(description="Add Woxpas to Claude Desktop (Docker mode).")
    ap.add_argument("--path", default=None, help="Config file to write (default: OS path)")
    ap.add_argument("--print", action="store_true", dest="print_only",
                    help="Print the JSON block instead of writing a file")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent
    compose_file = str(repo / "docker-compose.yml")

    env_file = repo / ".env"
    if not env_file.is_file() or "WOXPAS_DEV_TENANT_ID_DEFAULT" not in env_file.read_text():
        print("[woxpas] no tenant in .env yet — run the tenant step first "
              "(docker compose run --rm cli woxpas-cli init-tenant ...).", file=sys.stderr)
        return 1

    block = {
        "command": "docker",
        "args": ["compose", "-f", compose_file, "--profile", "mcp",
                 "run", "--rm", "-T", "mcp"],
    }

    if args.print_only:
        print(json.dumps({"mcpServers": {"woxpas": block}}, indent=2))
        return 0

    cfg_path = Path(args.path) if args.path else claude_config_path()
    existing: dict = {}
    if cfg_path.is_file():
        try:
            existing = json.loads(cfg_path.read_text() or "{}")
        except ValueError as e:
            print(f"[woxpas] couldn't parse {cfg_path}: {e}", file=sys.stderr)
            return 1
        if not isinstance(existing, dict):
            print(f"[woxpas] {cfg_path} isn't a JSON object — refusing to overwrite.", file=sys.stderr)
            return 1
        backup = cfg_path.with_suffix(cfg_path.suffix + ".woxpas-bak")
        backup.write_text(cfg_path.read_text())
        print(f"  backed up existing config -> {backup}")

    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        print(f"[woxpas] 'mcpServers' in {cfg_path} isn't an object — refusing to overwrite.", file=sys.stderr)
        return 1
    overwrote = "woxpas" in servers
    servers["woxpas"] = block

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"  {'updated' if overwrote else 'added'} the 'woxpas' MCP server in {cfg_path}")
    other = [k for k in servers if k != "woxpas"]
    if other:
        print(f"  (left your other MCP servers untouched: {', '.join(other)})")
    print("\n  Restart Claude Desktop to pick it up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
