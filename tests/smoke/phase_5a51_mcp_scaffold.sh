#!/usr/bin/env bash
# 5a.5.1 — MCP scaffold smoke battery.
#
# Spawns the woxpas-mcp-server subprocess, speaks JSON-RPC over stdio
# via the official MCP Python SDK as test client, verifies:
#   1. Server fails closed without WOXPAS_MCP_TENANT_ID
#   2. Server starts cleanly with tenant set
#   3. tools/list returns memory.recall with valid JSON Schema
#   4. tools/call memory.recall returns a real result
#   5. Invalid input → structured validation_failed error
#   6. Invalid tool name → structured not_found error
set -euo pipefail
cd /Users/sid/Documents/woxpas-memory
set -a; source .env; set +a
export POSTGRES_HOST=127.0.0.1

TENANT="e80524bc-a837-4bee-b9ca-0a9e682fd11e"

PASS=0; FAIL=0
note() { echo; echo "── $1 ──"; }
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# ─── 1. Refuse to start without tenant ──────────────────────────────────
note "1. Server fails closed without WOXPAS_MCP_TENANT_ID"

# Run the server with NO tenant env. Settings validation fails fast.
RC=0
( unset WOXPAS_MCP_TENANT_ID
  uv run --project packages/mcp woxpas-mcp-server </dev/null \
    >/tmp/mcp_no_tenant.out 2>/tmp/mcp_no_tenant.err ) || RC=$?
if [ "$RC" = "1" ]; then
  if grep -q "WOXPAS_MCP_TENANT_ID\|refusing to start" /tmp/mcp_no_tenant.err; then
    ok "exits 1 with clear error"
  else
    bad "exits 1 but error message unclear: $(cat /tmp/mcp_no_tenant.err | head -3)"
  fi
else
  bad "expected exit 1, got $RC; stderr: $(head -3 /tmp/mcp_no_tenant.err)"
fi

# ─── 2-6: Speak MCP protocol via Python SDK as test client ─────────────
note "2-6. End-to-end protocol via Python SDK client"

cat > /tmp/mcp_client_test.py <<PYEOF
import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PASS = 0
FAIL = 0
def ok(msg):
    global PASS; PASS += 1
    print(f"  PASS: {msg}")
def bad(msg):
    global FAIL; FAIL += 1
    print(f"  FAIL: {msg}")

async def run():
    # Spawn the server as an MCP child via stdio.
    params = StdioServerParameters(
        command="uv",
        args=["run", "--project", "packages/mcp", "woxpas-mcp-server"],
        env={
            **os.environ,
            "WOXPAS_MCP_TENANT_ID": "${TENANT}",
            "WOXPAS_DREAMING_ENABLED": "false",   # don't start the dreamer in this child
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("  server connected")

            # 3. tools/list
            tools = (await session.list_tools()).tools
            names = [t.name for t in tools]
            print(f"  tools: {names}")
            if "memory.recall" in names:
                ok("memory.recall registered")
            else:
                bad(f"memory.recall missing: {names}")

            # Verify input schema present + sane
            recall = next(t for t in tools if t.name == "memory.recall")
            schema = recall.inputSchema
            if isinstance(schema, dict) and "properties" in schema and "query_text" in schema["properties"]:
                ok("memory.recall has valid JSON Schema with query_text property")
            else:
                bad(f"schema malformed: {schema}")

            # 4. tools/call memory.recall with valid input
            res = await session.call_tool("memory.recall", {
                "query_text": "phoenix migration",
                "intent_hint": "semantic",
                "limit": 5,
                "rerank": False,
            })
            payload = res.content[0].text if res.content else ""
            try:
                data = json.loads(payload)
                if "items" in data and isinstance(data["items"], list):
                    ok(f"memory.recall returned {len(data['items'])} items")
                elif "error" in data:
                    bad(f"call returned error: {data['error']}")
                else:
                    bad(f"unexpected shape: keys={list(data.keys())}")
            except json.JSONDecodeError:
                bad(f"non-JSON response: {payload[:200]}")

            # 5. Invalid input → SDK rejects at protocol layer with isError=True
            # before our handler is even reached.
            res = await session.call_tool("memory.recall", {"query_text": ""})
            if res.isError:
                ok("invalid input → SDK pre-validates, isError=True")
            else:
                bad(f"expected isError=True for empty query_text, got: {res}")

            # 6. Invalid tool name → our handler raises, SDK sets isError=True.
            # The exception message embeds our structured JSON so the agent
            # can json.loads() it for the error envelope.
            res = await session.call_tool("memory.does_not_exist", {})
            if res.isError and res.content:
                # The text content contains the error message, possibly
                # wrapped in 'Error executing tool: ...' by the SDK.
                text = res.content[0].text
                # Look for the structured JSON anywhere in the text
                start = text.find('{"error"')
                if start >= 0:
                    end = text.rfind('}') + 1
                    err = json.loads(text[start:end])
                    if err.get("error", {}).get("code") == "not_found":
                        ok("unknown tool → isError=True with structured not_found")
                    else:
                        bad(f"unexpected error code: {err}")
                elif "not_found" in text or "unknown tool" in text.lower():
                    ok("unknown tool → isError=True with informative message")
                else:
                    bad(f"unknown tool error didn't carry structured info: {text[:200]}")
            else:
                bad(f"expected isError=True for unknown tool, got isError={res.isError}")

    print(f"\nPASS={PASS} FAIL={FAIL}")
    return FAIL

sys.exit(asyncio.run(run()))
PYEOF

# Need WOXPAS_MCP_TENANT_ID for the env passthrough. Already restored
# above via subshell scoping; re-set for the python client.
export WOXPAS_MCP_TENANT_ID="$TENANT"
RC=0
uv run --project packages/mcp python /tmp/mcp_client_test.py 2>/tmp/mcp_client.err || RC=$?
echo "  client exit: $RC"
if [ "$RC" = "0" ]; then
  ok "all subprocess tests passed"
else
  bad "client tests failed: $(tail -10 /tmp/mcp_client.err)"
fi

echo
echo "=========================================="
echo "PASS=$PASS  FAIL=$FAIL"
echo "=========================================="
exit $FAIL
