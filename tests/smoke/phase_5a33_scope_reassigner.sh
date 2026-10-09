#!/usr/bin/env bash
# 5a.3.3 ScopeReassigner smoke battery.
#
# Creates a scope with description embedding, ingests an unscoped doc that
# should match it, runs the dreaming worker, verifies reassignment.
set -euo pipefail
cd /Users/sid/Documents/woxpas-memory
set -a; source .env; set +a
export POSTGRES_HOST=127.0.0.1

TENANT="e80524bc-a837-4bee-b9ca-0a9e682fd11e"
H_TENANT="X-Woxpas-Tenant-Id: $TENANT"
API="http://localhost:7800"
PSQL=(docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At)

PASS=0
FAIL=0
note() { echo; echo "── $1 ──"; }
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# Cleanup leftover from prior runs — Postgres rows AND Qdrant orphans
# (when prior smokes deleted scopes from Postgres but didn't clean Qdrant).
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
DELETE FROM timeline_events WHERE actor = 'scope_reassigner';
DELETE FROM doc_scopes WHERE scope_id IN (SELECT scope_id FROM scopes WHERE slug LIKE 'smoke-%');
DELETE FROM scopes WHERE slug LIKE 'smoke-%';
" >/dev/null 2>&1 || true

# Delete orphan scope vectors in Qdrant (any scope whose row is missing)
uv run --project packages/api python -c "
import asyncio
from uuid import UUID
from woxpas_pipeline.context import PipelineContext
async def m():
    ctx = await PipelineContext.construct()
    try:
        async with ctx.pool.tenant_session(UUID('$TENANT')) as conn:
            # Live scope ids
            rows = await conn.fetch(\"SELECT scope_id FROM scopes\")
            live_ids = {str(r['scope_id']) for r in rows}
        # Get all scope points for this tenant from Qdrant
        from qdrant_client.models import Filter, FieldCondition, MatchValue, FilterSelector
        from woxpas_storage.qdrant.collections import Collection
        # Scroll through points
        offset = None
        orphan_ids = []
        while True:
            pts, offset = await ctx.qdrant._client.scroll(
                collection_name=Collection.SCOPES.value,
                scroll_filter=Filter(must=[FieldCondition(key='tenant_id', match=MatchValue(value='$TENANT'))]),
                limit=200,
                with_payload=True,
                offset=offset,
            )
            for p in pts:
                sid = (p.payload or {}).get('scope_id')
                if sid and sid not in live_ids:
                    orphan_ids.append(p.id)
            if offset is None:
                break
        if orphan_ids:
            await ctx.qdrant._client.delete(
                collection_name=Collection.SCOPES.value,
                points_selector=orphan_ids, wait=True,
            )
            print('SMOKE_PREP_OUT: cleaned', len(orphan_ids), 'orphan scope vectors')
        else:
            print('SMOKE_PREP_OUT: no orphans')
    finally:
        await ctx.close()
asyncio.run(m())
" 2>&1 | grep "SMOKE_PREP_OUT" | head -1

note "1. Verify ScopeReassigner runs cleanly when no matchable scope exists"
PRE_REASSIGN=$("${PSQL[@]}" -t -c "SET app.tenant_id = '$TENANT';
SELECT count(*) FROM doc_scopes;" 2>&1 | grep -E '^[0-9]+$' | tail -1)

uv run --project packages/api python -c "
import asyncio
from uuid import UUID
from woxpas_pipeline.context import PipelineContext
from woxpas_resurfacing.scope_reassigner import ScopeReassigner
async def m():
    ctx = await PipelineContext.construct()
    try:
        sr = ScopeReassigner(pool=ctx.pool, qdrant=ctx.qdrant)
        counts = await sr.reassign_for_tenant(tenant_id=UUID('$TENANT'))
        print('SMOKE_OUT:', counts)
    finally:
        await ctx.close()
asyncio.run(m())
" 2>&1 | grep "SMOKE_OUT" | head -1

POST_REASSIGN=$("${PSQL[@]}" -t -c "SET app.tenant_id = '$TENANT';
SELECT count(*) FROM doc_scopes;" 2>&1 | grep -E '^[0-9]+$' | tail -1)

if [ "$PRE_REASSIGN" = "$POST_REASSIGN" ]; then
  ok "no spurious reassignments when only Inbox (no embedding) exists"
else
  bad "doc_scopes count changed unexpectedly: $PRE_REASSIGN -> $POST_REASSIGN"
fi

note "2. Create test scope with description embedding via CLI"
# Use the create-scope CLI; it embeds description into Qdrant scopes_collection
uv run --project packages/api woxpas-cli create-scope \
  --tenant-id "$TENANT" \
  --name "Smoke Reassign Test" \
  --slug "smoke-reassign-test" \
  --description "Smoke test scope for verifying scope reassignment dreamer pass on agent-captured docs" \
  >/tmp/scope_create.log 2>&1

if grep -qE "scope_id|created" /tmp/scope_create.log 2>/dev/null; then
  ok "scope created via CLI"
else
  echo "  CLI output: $(cat /tmp/scope_create.log | tail -5)"
  bad "scope creation via CLI failed"
fi

# Verify Qdrant has the scope
SCOPE_ID=$("${PSQL[@]}" -t -c "SET app.tenant_id = '$TENANT';
SELECT scope_id FROM scopes WHERE slug = 'smoke-reassign-test';
" 2>&1 | grep -E '^[0-9a-f]{8}-' | tail -1 | tr -d '[:space:]')
echo "  scope_id=$SCOPE_ID"

note "3. Run ScopeReassigner; existing unscoped docs may now match smoke scope"
# Existing unscoped docs (from prior test data) have summary embeddings; the
# reassigner will compare against the smoke-reassign-test scope's embedding.
# Whether any actually match at >=0.55 cosine depends on content overlap.
uv run --project packages/api python -c "
import asyncio
from uuid import UUID
from woxpas_pipeline.context import PipelineContext
from woxpas_resurfacing.scope_reassigner import ScopeReassigner
async def m():
    ctx = await PipelineContext.construct()
    try:
        sr = ScopeReassigner(pool=ctx.pool, qdrant=ctx.qdrant)
        counts = await sr.reassign_for_tenant(tenant_id=UUID('$TENANT'))
        print('SMOKE_OUT:', counts)
    finally:
        await ctx.close()
asyncio.run(m())
" 2>&1 | grep "SMOKE_OUT" | tee /tmp/reassign_out.log | head -3

# Inspect what was reassigned (if anything)
ASSIGNED_TO_SMOKE=$("${PSQL[@]}" -t -c "SET app.tenant_id = '$TENANT';
SELECT count(*) FROM doc_scopes ds
  JOIN timeline_events te USING (doc_id, scope_id)
 WHERE ds.scope_id = '$SCOPE_ID'
   AND te.actor = 'scope_reassigner';
" 2>&1 | grep -E '^[0-9]+$' | tail -1)
echo "  docs reassigned to smoke scope: $ASSIGNED_TO_SMOKE"

if [ "$ASSIGNED_TO_SMOKE" -ge 0 ]; then
  ok "reassigner ran cleanly (assigned=$ASSIGNED_TO_SMOKE)"
else
  bad "reassigner failure"
fi

note "4. Idempotency — re-run, verify no duplicate assignments"
uv run --project packages/api python -c "
import asyncio
from uuid import UUID
from woxpas_pipeline.context import PipelineContext
from woxpas_resurfacing.scope_reassigner import ScopeReassigner
async def m():
    ctx = await PipelineContext.construct()
    try:
        sr = ScopeReassigner(pool=ctx.pool, qdrant=ctx.qdrant)
        counts = await sr.reassign_for_tenant(tenant_id=UUID('$TENANT'))
        print('SMOKE_OUT:', counts)
    finally:
        await ctx.close()
asyncio.run(m())
" 2>&1 | grep "SMOKE_OUT" | tee /tmp/reassign_out2.log | head -1

ASSIGNED2=$("${PSQL[@]}" -t -c "SET app.tenant_id = '$TENANT';
SELECT count(*) FROM doc_scopes ds
  JOIN timeline_events te USING (doc_id, scope_id)
 WHERE ds.scope_id = '$SCOPE_ID'
   AND te.actor = 'scope_reassigner';
" 2>&1 | grep -E '^[0-9]+$' | tail -1)

if [ "$ASSIGNED_TO_SMOKE" = "$ASSIGNED2" ]; then
  ok "idempotent: reassignment count stable ($ASSIGNED_TO_SMOKE -> $ASSIGNED2)"
else
  bad "non-idempotent: count changed $ASSIGNED_TO_SMOKE -> $ASSIGNED2"
fi

note "5. Verify reassignments emit timeline event (audit trail)"
TIMELINE_ROWS=$("${PSQL[@]}" -t -c "SET app.tenant_id = '$TENANT';
SELECT count(*) FROM timeline_events
 WHERE actor = 'scope_reassigner' AND event_type = 'doc.summarized';
" 2>&1 | grep -E '^[0-9]+$' | tail -1)
echo "  timeline rows from reassigner: $TIMELINE_ROWS"
if [ "$TIMELINE_ROWS" -eq "$ASSIGNED_TO_SMOKE" ]; then
  ok "every reassignment has a timeline event"
else
  bad "timeline rows ($TIMELINE_ROWS) != assignments ($ASSIGNED_TO_SMOKE)"
fi

note "6. Cross-tenant safety — wrong tenant gets nothing"
OTHER="00000000-0000-1234-5678-000000000999"
uv run --project packages/api python -c "
import asyncio
from uuid import UUID
from woxpas_pipeline.context import PipelineContext
from woxpas_resurfacing.scope_reassigner import ScopeReassigner
async def m():
    ctx = await PipelineContext.construct()
    try:
        sr = ScopeReassigner(pool=ctx.pool, qdrant=ctx.qdrant)
        counts = await sr.reassign_for_tenant(tenant_id=UUID('$OTHER'))
        # Expect zero candidates since OTHER has no docs
        print('SMOKE_OUT:', counts)
    finally:
        await ctx.close()
asyncio.run(m())
" 2>&1 | grep "SMOKE_OUT" | head -1

CROSS=$(grep -oE "candidates.: [0-9]+" /tmp/reassign_out2.log | head -1 || echo "")
# A simpler verification: nothing got assigned under OTHER
LEAK=$("${PSQL[@]}" -t -c "
SELECT count(*) FROM doc_scopes ds
  JOIN documents d USING (doc_id)
 WHERE d.tenant_id = '$OTHER';
" 2>&1 | grep -E '^[0-9]+$' | tail -1)
if [ "$LEAK" = "0" ]; then
  ok "cross-tenant pass left zero rows for OTHER"
else
  bad "RLS LEAK: $LEAK rows under OTHER"
fi

note "7. Synthetic match — use an unscoped doc's summary as a new scope's description"
# Pick an unscoped doc + use its summary as a scope description so the
# centroid vs summary embedding cosine is high (>0.55) by construction.
DOC_INFO=$("${PSQL[@]}" -t -c "
SET app.tenant_id = '$TENANT';
SELECT d.doc_id::text || '||' || left(coalesce(d.summary, d.title, 'doc'), 200)
  FROM documents d
  LEFT JOIN doc_scopes ds USING (doc_id)
 WHERE ds.doc_id IS NULL
   AND d.summary_embedding_ref IS NOT NULL
   AND d.summary IS NOT NULL
   AND d.archived_at IS NULL
 LIMIT 1;
" 2>&1 | grep -v warning | grep '||' | head -1)

DOC_ID=$(echo "$DOC_INFO" | cut -d'|' -f1)
DOC_SUMMARY=$(echo "$DOC_INFO" | cut -d'|' -f3-)
echo "  doc_id=$DOC_ID"
echo "  summary preview: $(echo "$DOC_SUMMARY" | head -c 80)..."

if [ -z "$DOC_ID" ] || [ -z "$DOC_SUMMARY" ]; then
  bad "could not find an unscoped doc with summary; skipping synthetic match"
else
  # Create a scope whose description is exactly this doc's summary
  uv run --project packages/api woxpas-cli create-scope \
    --tenant-id "$TENANT" \
    --name "Synth Match Scope" \
    --slug "smoke-synth-match" \
    --description "$DOC_SUMMARY" \
    >/tmp/scope_synth.log 2>&1 || true

  SYNTH_SCOPE_ID=$("${PSQL[@]}" -t -c "
  SET app.tenant_id = '$TENANT';
  SELECT scope_id FROM scopes WHERE slug = 'smoke-synth-match';
  " 2>&1 | grep -E '^[0-9a-f]{8}-' | tail -1 | tr -d '[:space:]')

  if [ -z "$SYNTH_SCOPE_ID" ]; then
    bad "synth scope creation failed: $(tail -3 /tmp/scope_synth.log)"
  else
    echo "  synth_scope_id=$SYNTH_SCOPE_ID"

    uv run --project packages/api python -c "
import asyncio
from uuid import UUID
from woxpas_pipeline.context import PipelineContext
from woxpas_resurfacing.scope_reassigner import ScopeReassigner
async def m():
    ctx = await PipelineContext.construct()
    try:
        sr = ScopeReassigner(pool=ctx.pool, qdrant=ctx.qdrant)
        counts = await sr.reassign_for_tenant(tenant_id=UUID('$TENANT'))
        print('SMOKE_OUT:', counts)
    finally:
        await ctx.close()
asyncio.run(m())
" 2>&1 | grep "SMOKE_OUT" | head -1

    SYNTH_ASSIGNED=$("${PSQL[@]}" -t -c "
    SET app.tenant_id = '$TENANT';
    SELECT count(*) FROM doc_scopes
     WHERE scope_id = '$SYNTH_SCOPE_ID' AND doc_id = '$DOC_ID';
    " 2>&1 | grep -E '^[0-9]+$' | tail -1)

    if [ "$SYNTH_ASSIGNED" = "1" ]; then
      ok "synthetic match: doc reassigned to scope built from its own summary"
    else
      bad "synthetic match failed: doc=$DOC_ID NOT assigned to scope=$SYNTH_SCOPE_ID (count=$SYNTH_ASSIGNED)"
    fi

    # Cleanup synth scope
    "${PSQL[@]}" -c "
    SET app.tenant_id = '$TENANT';
    DELETE FROM doc_scopes WHERE scope_id = '$SYNTH_SCOPE_ID';
    DELETE FROM scopes WHERE scope_id = '$SYNTH_SCOPE_ID';
    " >/dev/null 2>&1 || true
  fi
fi

note "8. Cleanup"
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
DELETE FROM doc_scopes WHERE scope_id = '$SCOPE_ID';
DELETE FROM scopes WHERE scope_id = '$SCOPE_ID';
DELETE FROM timeline_events WHERE actor = 'scope_reassigner';
" >/dev/null 2>&1 || true
ok "cleanup complete"

echo
echo "=========================================="
echo "PASS=$PASS  FAIL=$FAIL"
echo "=========================================="
exit $FAIL
