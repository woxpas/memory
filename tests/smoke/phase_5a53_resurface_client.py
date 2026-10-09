import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PASS=0; FAIL=0
def ok(m): global PASS; PASS+=1; print(f"  PASS: {m}")
def bad(m): global FAIL; FAIL+=1; print(f"  FAIL: {m}")
def section(t): print(f"\n── {t} ──")

TENANT = os.environ["WOXPAS_MCP_TENANT_ID"]

async def call(s, name, args):
    res = await s.call_tool(name, args)
    if res.isError:
        return {"_error": True, "_text": res.content[0].text if res.content else ""}
    text = res.content[0].text if res.content else "{}"
    return json.loads(text)

async def run():
    params = StdioServerParameters(
        command="uv",
        args=["run", "--project", "packages/mcp", "woxpas-mcp-server"],
        env={**os.environ, "WOXPAS_MCP_TENANT_ID": TENANT,
             "WOXPAS_DREAMING_ENABLED": "false"},
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            res_names = sorted(t.name for t in tools if t.name.startswith("resurface."))

            section("1. tools/list returns 12 resurface.* tools")
            if len(res_names) == 14:
                ok("all 14 resurface.* tools registered")
            else:
                bad(f"expected 14, got {len(res_names)}: {res_names}")

            section("2. resurface.get_context — Thinking Brain snapshot")
            d = await call(s, "resurface.get_context", {})
            if d.get("now") and "active_entities" in d and "open_threads" in d:
                ok(f"context: {len(d.get('active_entities',[]))} entities, "
                   f"{len(d.get('active_scopes',[]))} scopes")
            else:
                bad(f"context shape unexpected: keys={list(d.keys())[:10]}")

            section("3. resurface.get_patterns — Dormant Brain")
            d = await call(s, "resurface.get_patterns", {"limit": 10})
            if "patterns" in d:
                ok(f"patterns returned (count={len(d['patterns'])})")
            else:
                bad(f"patterns shape: {d}")

            section("4. resurface.record_observation — write")
            d = await call(s, "resurface.record_observation", {
                "agent_id": "smoke-5a53",
                "tool_called": "memory.recall",
                "outcome": {"success": True, "latency_ms": 50},
                "context": {"source": "5a.5.3 smoke"},
            })
            if d.get("recorded"):
                ok("observation recorded")
            else:
                bad(f"record failed: {d}")

            section("5. resurface.get_preferences — cross-agent prefs")
            d = await call(s, "resurface.get_preferences", {})
            if "answer_length_preference" in d:
                ok(f"prefs: answer_length={d['answer_length_preference']}, "
                   f"obs_count_30d={d.get('observation_count_30d')}")
            else:
                bad(f"prefs shape: keys={list(d.keys())[:8]}")

            section("6. resurface.get_tool_suggestions")
            d = await call(s, "resurface.get_tool_suggestions", {"limit": 5})
            if "suggestions" in d:
                ok(f"suggestions count={len(d['suggestions'])}, cold_start={d.get('cold_start')}")
            else:
                bad(f"suggestions shape: {d}")

            section("7. resurface.get_predicted_actions — memory-internal opportunities")
            d = await call(s, "resurface.get_predicted_actions", {"limit": 10})
            if "items" in d:
                ok(f"predicted_actions count={len(d['items'])}")
                # Verify urgency present on each
                if d["items"]:
                    has_urgency = all("urgency" in i for i in d["items"])
                    if has_urgency:
                        ok("every action carries urgency hint")
                    else:
                        bad("some items missing urgency")
            else:
                bad(f"predicted_actions shape: {d}")

            section("8. resurface.get_predicted_questions")
            d = await call(s, "resurface.get_predicted_questions", {"limit": 10})
            if "items" in d:
                ok(f"predicted_questions count={len(d['items'])}")
            else:
                bad(f"predicted_questions shape: {d}")

            section("9. resurface.post_prepared_action → list → approve → mark_executed")
            d = await call(s, "resurface.post_prepared_action", {
                "agent_id": "smoke-5a53",
                "action_kind": "test_action",
                "subject_label": "smoke 5a.5.3 lifecycle test",
                "draft_content": {"body": "test draft"},
                "requires_approval": True,
                "expires_in_hours": 1,
            })
            artifact_id = d.get("artifact_id")
            if artifact_id and d.get("status") == "drafted":
                ok(f"prepared_action posted: {artifact_id[:8]}…")
            else:
                bad(f"post failed: {d}")

            d = await call(s, "resurface.list_prepared_actions", {
                "agent_id": "smoke-5a53", "limit": 10,
            })
            found = any(i["artifact_id"] == artifact_id for i in d.get("items", []))
            if found:
                ok("list contains the new artifact")
            else:
                bad("new artifact not in list")

            d = await call(s, "resurface.approve_prepared_action", {
                "artifact_id": artifact_id,
            })
            if d.get("status") == "approved":
                ok("approve transitioned (status=approved)")
            else:
                bad(f"approve failed: {d}")

            d = await call(s, "resurface.mark_executed", {
                "artifact_id": artifact_id,
            })
            if d.get("status") == "executed":
                ok("mark_executed transitioned (status=executed)")
            else:
                bad(f"mark_executed failed: {d}")

            section("10. resurface.reject_prepared_action — separate artifact")
            d = await call(s, "resurface.post_prepared_action", {
                "agent_id": "smoke-5a53",
                "action_kind": "test_reject",
                "subject_label": "to reject",
                "draft_content": {},
                "requires_approval": True,
                "expires_in_hours": 1,
            })
            reject_id = d.get("artifact_id")
            d = await call(s, "resurface.reject_prepared_action", {
                "artifact_id": reject_id,
                "reason": "smoke 5a.5.3 reject test",
            })
            if d.get("status") == "rejected" and d.get("rejected_reason"):
                ok("reject transitioned + reason captured")
            else:
                bad(f"reject failed: {d}")

            section("11. Re-approve executed artifact → invalid_state error")
            d = await call(s, "resurface.approve_prepared_action", {
                "artifact_id": artifact_id,
            })
            if d.get("_error"):
                ok("re-approve on executed artifact → error (invalid_state)")
            else:
                bad(f"expected error, got: {d}")

            # Cleanup
            import asyncpg
            conn = await asyncpg.connect(
                host="127.0.0.1", port=5432,
                user=os.environ["POSTGRES_USER"],
                password=os.environ["POSTGRES_PASSWORD"],
                database=os.environ["POSTGRES_DB"],
            )
            await conn.execute(f"SET app.tenant_id = '{TENANT}'")
            await conn.execute(
                "DELETE FROM resurface_artifacts WHERE agent_id = 'smoke-5a53'"
            )
            await conn.execute(
                "DELETE FROM resurface_observations WHERE agent_id = 'smoke-5a53'"
            )
            await conn.close()

    print(f"\nPASS={PASS} FAIL={FAIL}")
    return FAIL

sys.exit(asyncio.run(run()))
