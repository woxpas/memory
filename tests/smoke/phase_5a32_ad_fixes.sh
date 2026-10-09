#!/usr/bin/env bash
# Phase 5a.3.2 — A-D fix smoke battery.
#
# Verifies the four bugs from the audit are actually fixed:
#   A. Stale predicted_action removed when condition resolves
#   B. Time bucketing uses tenant timezone, not server UTC
#   C. Empty/blank labels excluded from QuestionPreCacher subject pick
#   D. TTL-expired prepared_action transitions return 409
set -euo pipefail
cd /Users/sid/Documents/woxpas-memory
set -a; source .env; set +a
export POSTGRES_HOST=127.0.0.1

API="http://localhost:7800"
TENANT="e80524bc-a837-4bee-b9ca-0a9e682fd11e"
H_TENANT="X-Woxpas-Tenant-Id: $TENANT"

PSQL=(docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At)

PASS=0
FAIL=0
note() { echo; echo "── $1 ──"; }
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# Reset slate for this battery
"${PSQL[@]}" -c "SET app.tenant_id = '$TENANT';
DELETE FROM resurface_artifacts WHERE kind IN ('predicted_action','prepared_action');
DELETE FROM resurface_observations WHERE agent_id LIKE 'smoke%';
DELETE FROM tasks_deadlines WHERE title LIKE 'Smoke%';
DELETE FROM entities WHERE canonical_name IN ('   ', ' ', '');
" >/dev/null 2>&1 || true

# ─── A. Active-condition cleanup ──────────────────────────────────────────
note "A. Stale predicted_action removed when condition resolves"

TASK_ID=$("${PSQL[@]}" -t -c "SET app.tenant_id = '$TENANT';
INSERT INTO tasks_deadlines (tenant_id, title, kind, status, due_at)
VALUES ('$TENANT', 'Smoke A overdue task', 'task', 'open',
        now() - interval '5 days')
RETURNING task_id;" 2>&1 | grep -E '^[0-9a-f]{8}-' | head -1 | tr -d '[:space:]')
echo "  seeded task_id=$TASK_ID"

# First detect — should create an artifact for this task
uv run --project packages/api python -c "
import asyncio
from uuid import UUID
from woxpas_pipeline.context import PipelineContext
from woxpas_resurfacing.opportunities import OpportunityDetector
async def m():
    ctx = await PipelineContext.construct()
    try:
        await OpportunityDetector(pool=ctx.pool).detect_for_tenant(
            tenant_id=UUID('$TENANT'))
    finally:
        await ctx.close()
asyncio.run(m())
" >/dev/null 2>&1

# Verify artifact present for our task
PRESENT=$("${PSQL[@]}" -t -c "SET app.tenant_id = '$TENANT';
SELECT count(*) FROM resurface_artifacts
 WHERE kind = 'predicted_action'
   AND subject_signature = 'task_overdue:task:$TASK_ID';" \
  2>&1 | grep -E '^[0-9]+$' | tail -1)
[ "$PRESENT" = "1" ] && ok "task_overdue artifact created for seeded task" \
  || bad "expected 1 artifact, got $PRESENT"

# Mark the task done — condition no longer holds
"${PSQL[@]}" -c "SET app.tenant_id = '$TENANT';
UPDATE tasks_deadlines SET status = 'done' WHERE task_id = '$TASK_ID';
" >/dev/null 2>&1

# Re-run detector
uv run --project packages/api python -c "
import asyncio
from uuid import UUID
from woxpas_pipeline.context import PipelineContext
from woxpas_resurfacing.opportunities import OpportunityDetector
async def m():
    ctx = await PipelineContext.construct()
    try:
        counts = await OpportunityDetector(pool=ctx.pool).detect_for_tenant(
            tenant_id=UUID('$TENANT'))
        print(counts)
    finally:
        await ctx.close()
asyncio.run(m())
" 2>&1 | grep -i "stale_removed\|task_overdue" | tail -3

# Verify artifact GONE
GONE=$("${PSQL[@]}" -t -c "SET app.tenant_id = '$TENANT';
SELECT count(*) FROM resurface_artifacts
 WHERE kind = 'predicted_action'
   AND subject_signature = 'task_overdue:task:$TASK_ID';" \
  2>&1 | grep -E '^[0-9]+$' | tail -1)
[ "$GONE" = "0" ] && ok "stale artifact removed after task marked done" \
  || bad "stale artifact still present (count=$GONE)"

# Cleanup
"${PSQL[@]}" -c "SET app.tenant_id = '$TENANT';
DELETE FROM tasks_deadlines WHERE task_id = '$TASK_ID';" >/dev/null 2>&1

# ─── B. Tenant timezone bucketing ──────────────────────────────────────────
note "B. PatternDetector buckets in tenant timezone, not UTC"

# Save current timezone, set to America/New_York (UTC-4 or -5)
ORIG_TZ=$("${PSQL[@]}" -t -c "
SELECT COALESCE(timezone, 'UTC') FROM tenants WHERE tenant_id = '$TENANT';
" 2>&1 | grep -v warning | tail -1 | tr -d '[:space:]')
echo "  original tenant tz: $ORIG_TZ"

"${PSQL[@]}" -c "
UPDATE tenants SET timezone = 'America/New_York' WHERE tenant_id = '$TENANT';
" >/dev/null 2>&1

# Seed >= 10 observations all at 14:00 UTC.
# 14:00 UTC = 10:00 EDT (DST in May 2026) or 09:00 EST.
# zoneinfo handles DST automatically. We'll verify the bucketed hour is
# either 9 or 10 (depending on DST), but NOT 14.
"${PSQL[@]}" -c "SET app.tenant_id = '$TENANT';
INSERT INTO resurface_observations (
    tenant_id, agent_id, tool_called, observed_at, outcome, context
)
SELECT '$TENANT',
       'smoke-tz-agent',
       'memory.ask',
       (date_trunc('day', now() AT TIME ZONE 'UTC') - make_interval(days => gs)
         + interval '14 hours')::timestamptz,
       jsonb_build_object('success', true),
       '{}'::jsonb
  FROM generate_series(0, 14) AS gs;
" >/dev/null 2>&1

# Run pattern detector
uv run --project packages/api python -c "
import asyncio
from uuid import UUID
from woxpas_pipeline.context import PipelineContext
from woxpas_resurfacing.observations import PatternDetector
async def m():
    ctx = await PipelineContext.construct()
    try:
        await PatternDetector(pool=ctx.pool).update_preferences_for_tenant(
            tenant_id=UUID('$TENANT'))
    finally:
        await ctx.close()
asyncio.run(m())
" >/dev/null 2>&1

curl -sS -o /tmp/r.json -H "$H_TENANT" "$API/resurface/preferences" >/dev/null
HOUR=$(python3 -c "
import json; print(json.load(open('/tmp/r.json')).get('most_active_hour'))
")
echo "  most_active_hour reported: $HOUR"
# 14:00 UTC = 09:00 EST or 10:00 EDT. Either is correct (depends on DST).
# What matters is it's NOT 14.
if [ "$HOUR" = "9" ] || [ "$HOUR" = "10" ]; then
  ok "hour bucketed in tenant timezone (got $HOUR, not UTC 14)"
else
  bad "expected 9 or 10 (NY local time of UTC 14:00), got $HOUR"
fi

# Restore timezone
"${PSQL[@]}" -c "
UPDATE tenants SET timezone = '$ORIG_TZ' WHERE tenant_id = '$TENANT';
DELETE FROM resurface_observations WHERE agent_id = 'smoke-tz-agent';
" >/dev/null 2>&1

# ─── C. Empty/blank label filter ───────────────────────────────────────────
note "C. QuestionPreCacher subject pick skips empty labels"

# Insert an entity with empty name. v_entities exposes 'name' but the
# base table column is 'canonical_name'. Tenant-scoped; RLS-safe.
EMPTY_ID=$("${PSQL[@]}" -t -c "SET app.tenant_id = '$TENANT';
INSERT INTO entities (tenant_id, canonical_name, type, mention_count, last_mentioned_at)
VALUES ('$TENANT', '   ', 'person', 5, now())
RETURNING entity_id;
" 2>&1 | grep -E '^[0-9a-f]{8}-' | head -1 | tr -d '[:space:]')
echo "  seeded blank-name entity: $EMPTY_ID"

# Invoke _pick_subjects directly via Python. Use a sentinel marker so we
# can extract the JSON payload from interleaved structlog stderr.
PICKED=$(uv run --project packages/api python -c "
import asyncio, json
from uuid import UUID
from woxpas_pipeline.context import PipelineContext
from woxpas_resurfacing.question_cache import QuestionPreCacher

async def fake_synth(q, t, m): pass

async def m():
    ctx = await PipelineContext.construct()
    try:
        qpc = QuestionPreCacher(pool=ctx.pool, synthesize_fn=fake_synth)
        subjects = await qpc._pick_subjects(tenant_id=UUID('$TENANT'))
        print('SMOKE_OUT:' + json.dumps([str(s['id']) for s in subjects]))
    finally:
        await ctx.close()
asyncio.run(m())
" 2>&1 | grep '^SMOKE_OUT:' | sed 's/^SMOKE_OUT://')

# Verify the empty-name entity is NOT in the picked set
HAS_EMPTY=$(python3 -c "
import json
ids = json.loads('$PICKED')
print('$EMPTY_ID' in ids)
")
[ "$HAS_EMPTY" = "False" ] && ok "blank-name entity excluded from subject pick" \
  || bad "blank-name entity LEAKED into subject pick: $PICKED"

# Cleanup
"${PSQL[@]}" -c "SET app.tenant_id = '$TENANT';
DELETE FROM entities WHERE entity_id = '$EMPTY_ID';" >/dev/null 2>&1

# ─── D. TTL-expired transitions return 409 ─────────────────────────────────
note "D. Transitions on TTL-expired prepared_action return 409"

# POST a prepared_action with normal TTL
cat > /tmp/d_body.json <<EOF
{"agent_id":"smoke-d","action_kind":"x","draft_content":{"a":1},
 "requires_approval":true,"expires_in_hours":48}
EOF
ART_D=$(curl -sS -X POST -H "$H_TENANT" -H "Content-Type: application/json" \
  -d @/tmp/d_body.json "$API/resurface/prepared-action" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['artifact_id'])")
echo "  artifact_id=$ART_D"

# Manually expire it (push expires_at into the past)
"${PSQL[@]}" -c "SET app.tenant_id = '$TENANT';
UPDATE resurface_artifacts
   SET expires_at = now() - interval '1 hour'
 WHERE artifact_id = '$ART_D';" >/dev/null 2>&1

# Approve → 409
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST -H "$H_TENANT" \
  "$API/resurface/prepared-action/$ART_D/approve")
[ "$HTTP" = "409" ] && ok "approve on expired artifact → 409" \
  || bad "approve returned $HTTP (expected 409): $(cat /tmp/r.json)"

# Reject → 409
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" -d '{"reason":"x"}' \
  "$API/resurface/prepared-action/$ART_D/reject")
[ "$HTTP" = "409" ] && ok "reject on expired artifact → 409" \
  || bad "reject returned $HTTP (expected 409): $(cat /tmp/r.json)"

# Mark-executed → 409 (would also fail because not approved, but TTL alone
# also blocks it; this is defense-in-depth)
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST -H "$H_TENANT" \
  "$API/resurface/prepared-action/$ART_D/mark-executed")
[ "$HTTP" = "409" ] && ok "mark-executed on expired artifact → 409" \
  || bad "mark-executed returned $HTTP (expected 409): $(cat /tmp/r.json)"

# A NON-expired artifact should still work — sanity check
cat > /tmp/d_ok.json <<EOF
{"agent_id":"smoke-d-ok","action_kind":"x","draft_content":{},
 "requires_approval":false,"expires_in_hours":48}
EOF
ART_OK=$(curl -sS -X POST -H "$H_TENANT" -H "Content-Type: application/json" \
  -d @/tmp/d_ok.json "$API/resurface/prepared-action" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['artifact_id'])")
HTTP=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "$H_TENANT" \
  "$API/resurface/prepared-action/$ART_OK/approve")
[ "$HTTP" = "200" ] && ok "non-expired artifact still approves (sanity)" \
  || bad "sanity approve returned $HTTP"

# Cleanup
"${PSQL[@]}" -c "SET app.tenant_id = '$TENANT';
DELETE FROM resurface_artifacts WHERE agent_id LIKE 'smoke-d%';" >/dev/null 2>&1

# ─── Summary ───────────────────────────────────────────────────────────────
echo
echo "=========================================="
echo "PASS=$PASS  FAIL=$FAIL"
echo "=========================================="
exit $FAIL
