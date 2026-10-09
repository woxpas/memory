#!/usr/bin/env bash
#
# Woxpas — Docker-only installer. No uv, no Python 3.12 on the host; just Docker
# Desktop (running), bash, and a python3 (any version) for the Claude step.
#
#   ./install.sh "Your Name"
#
# Re-runnable. The one interactive moment is choosing a passphrase.
set -euo pipefail
cd "$(dirname "$0")"

NAME="${1:-}"
if [ -z "$NAME" ]; then
  read -rp "Your display name (used for your identity card): " NAME
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required — install Docker Desktop: https://www.docker.com/products/docker-desktop/"
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop doesn't look like it's running. Start it and re-run ./install.sh"
  exit 1
fi

echo
echo "== Building the Woxpas image (first run compiles the local LLM — a few minutes) =="
# Build the profile-gated app services explicitly — a bare `docker compose build`
# skips them, which can leave a stale image in place on a re-install.
docker compose --profile cli --profile app build

echo
echo "== Generating infrastructure secrets (.env) =="
touch .env                       # so Docker can bind-mount the file
# --build guards against reusing a stale image; -T keeps this non-interactive so
# the TTY stays free for the passphrase prompt below.
docker compose run --rm --build -T cli woxpas-cli init-env

echo
echo "== Starting datastores + workers =="
docker compose --profile app up -d --build

echo
echo "== Creating your tenant — choose a passphrase when prompted =="
docker compose run --rm cli woxpas-cli init-tenant --display-name "$NAME" --with-inbox --write-env

echo
echo "== Loading your key into the running services =="
docker compose --profile app up -d

echo
echo "== Wiring Claude Desktop =="
if command -v python3 >/dev/null 2>&1; then
  python3 scripts/install_claude_desktop.py || {
    echo
    echo "Couldn't auto-wire Claude Desktop. Print the block and paste it yourself:"
    echo "  python3 scripts/install_claude_desktop.py --print"
  }
else
  echo "python3 not found — install it (any version) and re-run, or wire Claude"
  echo "Desktop by hand. The MCP server command is:"
  echo "  docker compose -f \"$PWD/docker-compose.yml\" --profile mcp run --rm -T mcp"
fi

echo
echo "Done. Restart Claude Desktop, and Woxpas shows up as a set of memory tools."
echo "Stop everything with:  docker compose --profile app down"
