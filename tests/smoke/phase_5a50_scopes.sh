#!/usr/bin/env bash
# 5a.5.0 Group A — scope CRUD HTTP smoke battery.
#
# Covers: POST /scopes (create + embed), POST /documents/{id}/scopes (assign),
# DELETE /documents/{id}/scopes/{slug} (unassign), idempotency, error paths,
# cross-tenant RLS, and end-to-end with auto-assign downstream.
set -euo pipefail
cd /Users/sid/Documents/woxpas-memory
set -a; source .env; set +a
export POSTGRES_HOST=127.0.0.1

API="http://localhost:7800"
TENANT="e80524bc-a837-4bee-b9ca-0a9e682fd11e"
H_TENANT="X-Woxpas-Tenant-Id: $TENANT"
OTHER="00000000-0000-1234-5678-000000000999"
H_OTHER="X-Woxpas-Tenant-Id: $OTHER"

PSQL=(docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At)

PASS=0
FAIL=0
note() { echo; echo "── $1 ──"; }
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# Cleanup leftover smoke scopes (Postgres + Qdrant orphans)
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
DELETE FROM doc_scopes WHERE scope_id IN (SELECT scope_id FROM scopes WHERE slug LIKE 'smoke-grp-a%');
DELETE FROM scopes WHERE slug LIKE 'smoke-grp-a%';
" >/dev/null 2>&1 || true

uv run --project packages/api python -c "
import asyncio
from uuid import UUID
from woxpas_pipeline.context import PipelineContext
async def m():
    ctx = await PipelineContext.construct()
    try:
        async with ctx.pool.tenant_session(UUID('$TENANT')) as conn:
            rows = await conn.fetch('SELECT scope_id FROM scopes')
            live = {str(r['scope_id']) for r in rows}
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        from woxpas_storage.qdrant.collections import Collection
        offset = None
        orphans = []
        while True:
            pts, offset = await ctx.qdrant._client.scroll(
                collection_name=Collection.SCOPES.value,
                scroll_filter=Filter(must=[FieldCondition(key='tenant_id', match=MatchValue(value='$TENANT'))]),
                limit=200, with_payload=True, offset=offset,
            )
            for p in pts:
                sid = (p.payload or {}).get('scope_id')
                if sid and sid not in live:
                    orphans.append(p.id)
            if offset is None: break
        if orphans:
            await ctx.qdrant._client.delete(
                collection_name=Collection.SCOPES.value,
                points_selector=orphans, wait=True,
            )
    finally:
        await ctx.close()
asyncio.run(m())
" >/dev/null 2>&1 || true

# ─── 1. Create scope ────────────────────────────────────────────────────
note "1. POST /scopes — create with embedding"

cat > /tmp/cs.json <<EOF
{
  "name": "Smoke Group A Project",
  "description": "Test scope for 5a.5.0 Group A scope CRUD smoke",
  "slug": "smoke-grp-a",
  "color": "#ff5500",
  "icon": "rocket"
}
EOF
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d @/tmp/cs.json "$API/scopes")
SCOPE_ID=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['scope_id'])" 2>/dev/null || echo "")
[ "$HTTP" = "201" ] && [ -n "$SCOPE_ID" ] && ok "POST /scopes returns 201 with scope_id" \
  || bad "POST /scopes returned $HTTP: $(cat /tmp/r.json)"

EMBEDDED=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['embedded'])" 2>/dev/null)
[ "$EMBEDDED" = "True" ] && ok "scope embedded into Qdrant" \
  || bad "embedding flag was $EMBEDDED"

ALREADY=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['already_existed'])" 2>/dev/null)
[ "$ALREADY" = "False" ] && ok "fresh scope (already_existed=False)" \
  || bad "already_existed=$ALREADY (expected False)"

# ─── 2. Idempotent re-create ────────────────────────────────────────────
note "2. POST /scopes with same slug — idempotent upsert"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d @/tmp/cs.json "$API/scopes")
SCOPE_ID2=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['scope_id'])")
ALREADY2=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['already_existed'])")
[ "$SCOPE_ID" = "$SCOPE_ID2" ] && ok "same scope_id returned (upsert by slug)" \
  || bad "different scope_id: $SCOPE_ID -> $SCOPE_ID2"
[ "$ALREADY2" = "True" ] && ok "already_existed=True on re-create" \
  || bad "already_existed=$ALREADY2 (expected True)"

# ─── 3. Create with embed=false (catch-all pattern) ─────────────────────
note "3. POST /scopes embed=false — invisible to auto-assign"
cat > /tmp/cs2.json <<EOF
{"name":"Smoke Catch All","slug":"smoke-grp-a-catchall","embed":false}
EOF
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" -d @/tmp/cs2.json "$API/scopes")
EMBEDDED=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['embedded'])")
[ "$HTTP" = "201" ] && [ "$EMBEDDED" = "False" ] && ok "embed=false honored" \
  || bad "embed=false returned $HTTP, embedded=$EMBEDDED"

# ─── 4. Assign to scope ─────────────────────────────────────────────────
note "4. POST /documents/{id}/scopes — assign existing doc"

# Pick an existing unscoped doc
DOC_ID=$("${PSQL[@]}" -t -c "
SET app.tenant_id = '$TENANT';
SELECT d.doc_id FROM documents d
  LEFT JOIN doc_scopes ds USING (doc_id)
 WHERE ds.doc_id IS NULL AND d.archived_at IS NULL
 LIMIT 1;
" 2>&1 | grep -E '^[0-9a-f]{8}-' | tail -1 | tr -d '[:space:]')
echo "  picked doc_id=$DOC_ID"

HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d "{\"scope_slug\":\"smoke-grp-a\",\"assigned_by\":\"agent\"}" \
  "$API/documents/$DOC_ID/scopes")
INSERTED=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['inserted'])")
[ "$HTTP" = "200" ] && [ "$INSERTED" = "True" ] && ok "assignment created (inserted=True)" \
  || bad "assign returned $HTTP, inserted=$INSERTED: $(cat /tmp/r.json)"

# ─── 5. Idempotent re-assign ────────────────────────────────────────────
note "5. POST same assignment again — inserted=False"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d "{\"scope_slug\":\"smoke-grp-a\"}" "$API/documents/$DOC_ID/scopes")
INSERTED=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['inserted'])")
[ "$HTTP" = "200" ] && [ "$INSERTED" = "False" ] && ok "re-assign idempotent" \
  || bad "re-assign returned $HTTP, inserted=$INSERTED"

# ─── 6. Assign to non-existent scope returns 404 ────────────────────────
note "6. Assign to missing scope → 404"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d '{"scope_slug":"does-not-exist"}' "$API/documents/$DOC_ID/scopes")
[ "$HTTP" = "404" ] && ok "404 for missing scope" \
  || bad "expected 404, got $HTTP: $(cat /tmp/r.json)"

# ─── 7. Assign to non-existent doc returns 404 ──────────────────────────
note "7. Assign for missing doc → 404"
FAKE_DOC="00000000-0000-0000-0000-000000000000"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d "{\"scope_slug\":\"smoke-grp-a\"}" "$API/documents/$FAKE_DOC/scopes")
[ "$HTTP" = "404" ] && ok "404 for missing doc" \
  || bad "expected 404, got $HTTP: $(cat /tmp/r.json)"

# ─── 8. Cross-tenant assign blocked ─────────────────────────────────────
note "8. Cross-tenant assign — TENANT's doc not visible to OTHER"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_OTHER" -H "Content-Type: application/json" \
  -d "{\"scope_slug\":\"smoke-grp-a\"}" "$API/documents/$DOC_ID/scopes")
# OTHER tenant: scopes table sees no smoke-grp-a (different tenant) → 404
[ "$HTTP" = "404" ] && ok "OTHER tenant gets 404 (RLS hides scope)" \
  || bad "expected 404, got $HTTP: $(cat /tmp/r.json)"

# ─── 9. Unassign ────────────────────────────────────────────────────────
note "9. DELETE /documents/{id}/scopes/{slug}"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X DELETE \
  -H "$H_TENANT" "$API/documents/$DOC_ID/scopes/smoke-grp-a")
INSERTED=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['inserted'])")
[ "$HTTP" = "200" ] && [ "$INSERTED" = "True" ] && ok "unassign returned inserted=True (row removed)" \
  || bad "unassign returned $HTTP inserted=$INSERTED"

# ─── 10. Idempotent unassign ────────────────────────────────────────────
note "10. DELETE again → inserted=False (idempotent no-op)"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X DELETE \
  -H "$H_TENANT" "$API/documents/$DOC_ID/scopes/smoke-grp-a")
INSERTED=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['inserted'])")
[ "$HTTP" = "200" ] && [ "$INSERTED" = "False" ] && ok "idempotent unassign (no-op)" \
  || bad "unassign idempotent returned $HTTP inserted=$INSERTED"

# ─── 11. Validation: empty name → 422 ──────────────────────────────────
note "11. Validation — empty name"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d '{"name":""}' "$API/scopes")
[ "$HTTP" = "422" ] && ok "empty name → 422 validation error" \
  || bad "empty name returned $HTTP: $(cat /tmp/r.json)"

# ─── 12. Recall finds doc under scope after assign ──────────────────────
note "12. End-to-end: assign + recall with scope filter"
# Re-assign so we can recall it
curl -sS -X POST -H "$H_TENANT" -H "Content-Type: application/json" \
  -d "{\"scope_slug\":\"smoke-grp-a\"}" "$API/documents/$DOC_ID/scopes" >/dev/null

# Recall with intent=type_listing (lists files in scope) — deterministic
# (no semantic ranking) so scope filter is the sole driver.
cat > /tmp/recall.json <<EOF
{"query_text":"files","intent_hint":"type_listing","filters":{"scope_slugs":["smoke-grp-a"]},"limit":50,"rerank":false}
EOF
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d @/tmp/recall.json "$API/recall")
HAS_DOC=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
items = d.get('items', [])
# ResultItem uses 'id' for document items
ids = {i.get('id') for i in items}
print('$DOC_ID' in ids)
" 2>/dev/null)
[ "$HTTP" = "200" ] && [ "$HAS_DOC" = "True" ] && ok "recall (type_listing) with scope filter finds the doc" \
  || bad "recall returned $HTTP, has_doc=$HAS_DOC; resp head: $(head -c 400 /tmp/r.json)"

# ─── 13. Cleanup ────────────────────────────────────────────────────────
note "13. Cleanup"
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
DELETE FROM doc_scopes WHERE scope_id IN (SELECT scope_id FROM scopes WHERE slug LIKE 'smoke-grp-a%');
DELETE FROM scopes WHERE slug LIKE 'smoke-grp-a%';
" >/dev/null 2>&1 || true
ok "cleanup complete"

echo
echo "=========================================="
echo "PASS=$PASS  FAIL=$FAIL"
echo "=========================================="
exit $FAIL
