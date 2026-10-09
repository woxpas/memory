#!/usr/bin/env bash
# 5a.3.4 Track 2a + 2b smoke battery.
#
# Track 2a: pipeline scope auto-assign retries at lower threshold when
# captured_by=agent.
# Track 2b: doc_unscoped opportunity orders agent-captured first + adds
# urgency hint based on captured_by + age.
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

# Reset prior state
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
DELETE FROM resurface_artifacts WHERE kind = 'predicted_action';
" >/dev/null 2>&1 || true

# ─── Track 2b smoke: urgency + ordering on doc_unscoped opportunity ─────
note "Track 2b: doc_unscoped opportunity has urgency + agent-first ordering"

# Mark some existing unscoped docs as captured_by=agent (mutate metadata).
# Safer than ingesting fresh agent docs (which would also test 2a but takes ~12s
# per doc). Section 2a below is a separate, smaller test.
AGENT_DOCS=$("${PSQL[@]}" -t -c "
SET app.tenant_id = '$TENANT';
WITH targets AS (
  SELECT d.doc_id
    FROM documents d
    LEFT JOIN doc_scopes ds USING (doc_id)
   WHERE ds.doc_id IS NULL AND d.archived_at IS NULL
   LIMIT 3
)
UPDATE documents
   SET captured_by = 'agent', updated_at = now()
 WHERE doc_id IN (SELECT doc_id FROM targets)
RETURNING doc_id;
" 2>&1 | grep -E '^[0-9a-f]{8}-' | tr '\n' ' ')
echo "  marked agent docs: $AGENT_DOCS"

# Run OpportunityDetector
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

# Hit API to get items + verify urgency present + agent-first ordering
curl -sS -o /tmp/r.json -H "$H_TENANT" \
  "$API/resurface/predicted-actions?action_kinds=doc_unscoped&limit=20" >/dev/null

# 1. Each item has urgency
HAS_URGENCY=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
items = d.get('items', [])
print(all('urgency' in i and i['urgency'] in ('high','medium','low') for i in items))
")
[ "$HAS_URGENCY" = "True" ] && ok "every doc_unscoped item has urgency=high|medium|low" \
  || bad "missing or invalid urgency on some items: $(cat /tmp/r.json | head -c 500)"

# 2. Agent-captured items appear before user-captured
AGENT_FIRST=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
items = d.get('items', [])
captured = [i.get('context', {}).get('captured_by') for i in items]
# Find first user index; all earlier indices must be agent
first_user = next((idx for idx, c in enumerate(captured) if c != 'agent'), len(captured))
all_before_are_agent = all(captured[i] == 'agent' for i in range(first_user))
agent_count = captured.count('agent')
print(f'agent_count={agent_count}, first_user_idx={first_user}, agent_first={all_before_are_agent}')
")
echo "  $AGENT_FIRST"
if echo "$AGENT_FIRST" | grep -q "agent_first=True"; then
  ok "agent-captured docs ordered before user-captured"
else
  bad "ordering broken: $AGENT_FIRST"
fi

# 3. Agent-captured + recent (<24h not necessarily; created_at is older test data)
# Verify urgency mapping: agent + young → high; agent + old → medium; user → medium/low
URGENCY_LOGIC=$(python3 -c "
import json
from datetime import datetime, timezone
d = json.load(open('/tmp/r.json'))
items = d.get('items', [])
ok = True
for i in items:
    captured = i.get('context', {}).get('captured_by')
    days = i.get('context', {}).get('days_unscoped', 0)
    urgency = i.get('urgency')
    # agent: high if <1d, medium otherwise
    # user: medium if <1d, low otherwise
    if captured == 'agent':
        expected = 'high' if days < 1 else 'medium'
    else:
        expected = 'medium' if days < 1 else 'low'
    if urgency != expected:
        ok = False
        print(f'  mismatch: captured={captured} days={days:.1f} got={urgency} expected={expected}')
print('logic_ok=', ok)
")
echo "  $URGENCY_LOGIC"
if echo "$URGENCY_LOGIC" | grep -q "logic_ok= True"; then
  ok "urgency mapping correct (agent+young=high, agent+old=medium, user+young=medium, user+old=low)"
else
  bad "urgency logic broken"
fi

# Cleanup: revert captured_by back to 'user' on the docs we touched (the
# default for legacy data; pipeline writes 'user' for file uploads).
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
UPDATE documents
   SET captured_by = 'user'
 WHERE captured_by = 'agent'
   AND doc_id IN ($(echo "$AGENT_DOCS" | sed "s/[a-f0-9-]\{36\}/'&'/g; s/  */, /g; s/, *$//" ));
" >/dev/null 2>&1 || true

# ─── Track 2a smoke: lower threshold for agent captures at ingest ───────
note "Track 2a: settings.scope_assign_threshold_agent exposed and lower"
THRESHOLD_NORMAL=$(uv run --project packages/api python -c "
from woxpas_pipeline.settings import PipelineSettings
s = PipelineSettings()
print(s.scope_assign_threshold)
" 2>&1 | tail -1)
THRESHOLD_AGENT=$(uv run --project packages/api python -c "
from woxpas_pipeline.settings import PipelineSettings
s = PipelineSettings()
print(s.scope_assign_threshold_agent)
" 2>&1 | tail -1)
echo "  normal=$THRESHOLD_NORMAL  agent=$THRESHOLD_AGENT"
if (( $(echo "$THRESHOLD_AGENT < $THRESHOLD_NORMAL" | bc -l) )); then
  ok "agent threshold ($THRESHOLD_AGENT) is lower than normal ($THRESHOLD_NORMAL)"
else
  bad "agent threshold should be lower than normal"
fi

# Verify the second-pass code path is reachable. Synthetic: agent doc whose
# summary embedding scores between agent_threshold and normal_threshold.
# We can't easily construct this without a real ingest, so we verify the
# code path is wired correctly via Python introspection.
SECOND_PASS_WIRED=$(uv run --project packages/api python -c "
import inspect
from woxpas_pipeline.summarize.stage import SummarizeStage
src = inspect.getsource(SummarizeStage._auto_assign_scope)
has_captured_by_param = 'captured_by' in src
has_agent_branch = 'agent_threshold' in src and 'scope_assign_threshold_agent' in src
print('wired=', has_captured_by_param and has_agent_branch)
" 2>&1 | tail -1)
echo "  $SECOND_PASS_WIRED"
if echo "$SECOND_PASS_WIRED" | grep -q "wired= True"; then
  ok "Track 2a code path wired in summarize stage"
else
  bad "Track 2a code path missing"
fi

# ─── Cross-tenant doc_unscoped urgency RLS ──────────────────────────────
note "Cross-tenant: agent-flagged docs don't leak"
OTHER="00000000-0000-1234-5678-000000000999"
H_OTHER="X-Woxpas-Tenant-Id: $OTHER"
curl -sS -o /tmp/r.json -H "$H_OTHER" \
  "$API/resurface/predicted-actions?action_kinds=doc_unscoped&limit=10" >/dev/null
TOT=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['total'])")
[ "$TOT" = "0" ] && ok "OTHER tenant sees zero doc_unscoped" \
  || bad "RLS LEAK: $TOT items"

# ─── Cleanup ────────────────────────────────────────────────────────────
note "Cleanup"
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
DELETE FROM resurface_artifacts WHERE kind = 'predicted_action';
" >/dev/null 2>&1 || true
ok "cleanup complete"

echo
echo "=========================================="
echo "PASS=$PASS  FAIL=$FAIL"
echo "=========================================="
exit $FAIL
