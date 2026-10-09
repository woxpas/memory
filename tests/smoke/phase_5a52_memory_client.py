import asyncio
import json
import os
import sys
from uuid import UUID

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
        text = res.content[0].text if res.content else ""
        return {"_error": True, "_text": text}
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
            names = sorted(t.name for t in tools)
            print(f"  registered: {len(names)} tools")

            section("1. tools/list returns 13 memory.* tools")
            mem = [n for n in names if n.startswith("memory.")]
            if len(mem) == 13:
                ok("all 13 memory.* tools registered")
            else:
                bad(f"expected 13, got {len(mem)}: {mem}")

            section("2. memory.recall → semantic search returns items")
            d = await call(s, "memory.recall", {
                "query_text": "phoenix migration",
                "intent_hint": "semantic",
                "limit": 5, "rerank": False,
            })
            if d.get("items"):
                ok(f"recall returned {len(d['items'])} items")
            else:
                bad(f"recall empty: {d}")

            section("3. memory.create_scope → scope_id, embedded=True")
            d = await call(s, "memory.create_scope", {
                "name": "MCP Smoke Tools Test",
                "slug": "mcp-smoke-tools",
                "description": "Smoke test scope for 5a.5.2",
            })
            scope_id = d.get("scope_id")
            if scope_id and d.get("embedded"):
                ok(f"created scope {d['slug']} (embedded)")
            else:
                bad(f"create_scope failed: {d}")

            section("4. memory.ingest → text capture, returns doc_id")
            import time
            ts = str(time.time())
            d = await call(s, "memory.ingest", {
                "content_text": f"MCP smoke ingest test #{ts}. Phoenix project notes.",
                "suggested_title": f"MCP smoke #{ts}",
                "suggested_scope_slugs": ["mcp-smoke-tools"],
            })
            doc_id = d.get("doc_id")
            if doc_id and d.get("status") == "received":
                ok(f"ingest accepted: doc_id={doc_id[:8]}…")
            else:
                bad(f"ingest failed: {d}")

            section("5. memory.assign_to_scope (idempotent)")
            # First find a doc that exists
            d = await call(s, "memory.recall", {
                "query_text": "files", "intent_hint": "type_listing",
                "limit": 1, "rerank": False,
            })
            existing_doc_id = d["items"][0]["id"] if d.get("items") else None
            if not existing_doc_id:
                bad("no doc to assign")
            else:
                d = await call(s, "memory.assign_to_scope", {
                    "doc_id": existing_doc_id, "scope_slug": "mcp-smoke-tools",
                })
                if d.get("inserted") in (True, False):  # both valid
                    ok(f"assign_to_scope: inserted={d['inserted']}")
                else:
                    bad(f"assign failed: {d}")

                section("6. memory.unassign_from_scope")
                d = await call(s, "memory.unassign_from_scope", {
                    "doc_id": existing_doc_id, "scope_slug": "mcp-smoke-tools",
                })
                if d.get("inserted") in (True, False):
                    ok(f"unassign: removed={d['inserted']}")
                else:
                    bad(f"unassign failed: {d}")

            section("7. memory.assign_to_scope → 404 on unknown scope")
            if existing_doc_id:
                d = await call(s, "memory.assign_to_scope", {
                    "doc_id": existing_doc_id, "scope_slug": "does-not-exist",
                })
                if d.get("_error"):
                    ok("not_found surfaced for unknown scope")
                else:
                    bad(f"expected error, got: {d}")

            section("8. memory.archive_document + unarchive (idempotent pair)")
            if existing_doc_id:
                d = await call(s, "memory.archive_document", {
                    "doc_id": existing_doc_id,
                    "reason": "smoke test"
                })
                if d.get("state_changed") and d.get("archived_at"):
                    ok("archive transitioned (archived_at present)")
                else:
                    bad(f"archive failed: {d}")
                d = await call(s, "memory.unarchive_document", {
                    "doc_id": existing_doc_id,
                })
                if d.get("state_changed") and d.get("archived_at") is None:
                    ok("unarchive transitioned (archived_at cleared)")
                else:
                    bad(f"unarchive failed: {d}")

            section("9. memory.get_chunks → batch hydrate")
            # Get chunk_ids from a recent recall
            d = await call(s, "memory.recall", {
                "query_text": "phoenix migration",
                "intent_hint": "semantic",
                "limit": 3, "rerank": False,
            })
            chunk_ids = [i["id"] for i in d.get("items", []) if i.get("kind") == "chunk"][:2]
            if chunk_ids:
                d = await call(s, "memory.get_chunks", {"chunk_ids": chunk_ids})
                if d.get("chunks") and d.get("returned") == len(chunk_ids):
                    ok(f"hydrated {d['returned']} chunks")
                else:
                    bad(f"get_chunks failed: {d}")
            else:
                bad("no chunk_ids available to hydrate")

            section("10. memory.get_document_full")
            if existing_doc_id:
                d = await call(s, "memory.get_document_full", {
                    "doc_id": existing_doc_id,
                    "include_chunks": True,
                })
                if d.get("doc_id") == existing_doc_id and "chunks" in d:
                    ok(f"got doc + {len(d['chunks'])} chunks")
                else:
                    bad(f"get_document_full failed: keys={list(d.keys())}")

            section("11. memory.list_conflicts (state=open)")
            d = await call(s, "memory.list_conflicts", {"state": "open"})
            if d.get("total", -1) >= 0 and isinstance(d.get("items"), list):
                ok(f"list_conflicts returned total={d['total']}")
                conflict_id = d["items"][0]["conflict_id"] if d["items"] else None
            else:
                bad(f"list_conflicts failed: {d}")
                conflict_id = None

            section("12. memory.get_conflict → suggested_shapes present")
            if conflict_id:
                d = await call(s, "memory.get_conflict", {"conflict_id": conflict_id})
                shapes = d.get("suggested_shapes", [])
                if {"pick","coexistence","progression","dismissed"}.issubset(set(shapes)):
                    ok("all 4 shapes suggested")
                else:
                    bad(f"shapes incomplete: {shapes}")
            else:
                print("  (skipped — no conflicts)")

            section("13. memory.resolve_conflict pick → validation_failed without winner/loser")
            if conflict_id:
                d = await call(s, "memory.resolve_conflict", {
                    "conflict_id": conflict_id, "kind": "pick",
                })
                # SDK may catch validation error or our handler may raise
                # ValidationFailed. Either way isError=True.
                if d.get("_error"):
                    ok("pick without winner/loser → error (validation)")
                else:
                    bad(f"expected error, got: {d}")

            section("14. memory.ask (DeepSeek cloud-gated, brief)")
            # Enable cloud for personal data temporarily — required for
            # DeepSeek to operate on existing test data
            import asyncpg
            conn = await asyncpg.connect(
                host="127.0.0.1", port=5432,
                user=os.environ["POSTGRES_USER"],
                password=os.environ["POSTGRES_PASSWORD"],
                database=os.environ["POSTGRES_DB"],
            )
            await conn.execute(
                "UPDATE tenants SET allow_cloud_for_personal_data=true WHERE tenant_id=$1",
                UUID(TENANT),
            )
            try:
                d = await call(s, "memory.ask", {
                    "query_text": "phoenix migration",
                    "model_name": "deepseek-chat",
                    "max_tokens": 200,
                    "max_sources": 4,
                    "persona": "Be concise.",
                })
                if d.get("answer_clean") and d.get("citations"):
                    ok(f"ask synthesized {len(d['answer_clean'])} chars w/ "
                       f"{len(d['citations'])} citations")
                else:
                    bad(f"ask failed: keys={list(d.keys())}")
            finally:
                await conn.execute(
                    "UPDATE tenants SET allow_cloud_for_personal_data=false WHERE tenant_id=$1",
                    UUID(TENANT),
                )
                await conn.close()

            # Cleanup
            import asyncpg
            conn = await asyncpg.connect(
                host="127.0.0.1", port=5432,
                user=os.environ["POSTGRES_USER"],
                password=os.environ["POSTGRES_PASSWORD"],
                database=os.environ["POSTGRES_DB"],
            )
            await conn.execute(f"SET app.tenant_id = '{TENANT}'")
            await conn.execute("DELETE FROM doc_scopes WHERE scope_id IN (SELECT scope_id FROM scopes WHERE slug='mcp-smoke-tools')")
            await conn.execute("DELETE FROM scopes WHERE slug='mcp-smoke-tools'")
            if doc_id:
                await conn.execute(f"DELETE FROM documents WHERE doc_id = '{doc_id}'")
            await conn.close()

    print(f"\nPASS={PASS} FAIL={FAIL}")
    return FAIL

sys.exit(asyncio.run(run()))
