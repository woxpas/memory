#!/usr/bin/env bash
# 5a.5+ — NOYU-supporting MCP writes: upsert_persona + record_decision.
set -euo pipefail
cd /Users/sid/Documents/woxpas-memory
set -a; source .env; set +a
export POSTGRES_HOST=127.0.0.1

API="http://localhost:7800"
TENANT="e80524bc-a837-4bee-b9ca-0a9e682fd11e"
H_TENANT="X-Woxpas-Tenant-Id: $TENANT"
PSQL=(docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" postgres \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At)

PASS=0; FAIL=0
note() { echo; echo "── $1 ──"; }
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# Reset prior state
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
UPDATE resurface_user_preferences SET persona_tone = '{}'::jsonb
 WHERE tenant_id = '$TENANT';
DELETE FROM resurface_observations
 WHERE agent_id = 'smoke-noyu-writes';
" >/dev/null 2>&1 || true

# ─── upsert_persona ─────────────────────────────────────────────────────
note "1. POST /resurface/persona — initial write"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d '{"persona_patch":{"default":{"email":{"register":"professional","opener":"Hi {name},","sign_off":"Best,\nSid","avg_words":50}}}}' \
  "$API/resurface/persona")
HAS_EMAIL=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
pt = d.get('persona_tone') or {}
print(pt.get('default', {}).get('email', {}).get('register'))
")
[ "$HTTP" = "200" ] && [ "$HAS_EMAIL" = "professional" ] \
  && ok "initial persona written" \
  || bad "expected register=professional, got $HTTP / $HAS_EMAIL"

note "2. Deep-merge — adding slack while preserving email"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d '{"persona_patch":{"default":{"slack":{"register":"casual","emoji_use":"frequent"}}}}' \
  "$API/resurface/persona")
EMAIL_REGISTER=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
pt = d.get('persona_tone') or {}
print(pt.get('default', {}).get('email', {}).get('register'))
")
SLACK_REGISTER=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
pt = d.get('persona_tone') or {}
print(pt.get('default', {}).get('slack', {}).get('register'))
")
[ "$EMAIL_REGISTER" = "professional" ] && [ "$SLACK_REGISTER" = "casual" ] \
  && ok "deep-merge preserved email + added slack" \
  || bad "merge broken: email=$EMAIL_REGISTER slack=$SLACK_REGISTER"

note "3. Per-relationship override"
curl -sS -o /tmp/r.json -X POST -H "$H_TENANT" -H "Content-Type: application/json" \
  -d '{"persona_patch":{"per_relationship":{"7fccb4ae-a2c0-4329-84eb-e4de4b4402df":{"email":{"register":"formal"}}}}}' \
  "$API/resurface/persona" >/dev/null
RELATION=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
pt = d.get('persona_tone') or {}
print(pt.get('per_relationship', {}).get('7fccb4ae-a2c0-4329-84eb-e4de4b4402df', {}).get('email', {}).get('register'))
")
[ "$RELATION" = "formal" ] && ok "per-relationship override stored" \
  || bad "per_relationship missing: $RELATION"

note "4. Read back via GET /resurface/preferences"
curl -sS -o /tmp/r.json -H "$H_TENANT" "$API/resurface/preferences" >/dev/null
EMAIL=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
pt = d.get('persona_tone') or {}
print(pt.get('default', {}).get('email', {}).get('register'))
")
[ "$EMAIL" = "professional" ] && ok "preferences endpoint surfaces persona" \
  || bad "preferences missing persona: $EMAIL"

note "5. Drop a key by passing null"
curl -sS -o /tmp/r.json -X POST -H "$H_TENANT" -H "Content-Type: application/json" \
  -d '{"persona_patch":{"default":{"slack":null}}}' \
  "$API/resurface/persona" >/dev/null
SLACK_AFTER=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
pt = d.get('persona_tone') or {}
print(pt.get('default', {}).get('slack'))
")
[ "$SLACK_AFTER" = "None" ] && ok "null value drops the key" \
  || bad "null didn't drop: slack=$SLACK_AFTER"

# ─── record_decision ────────────────────────────────────────────────────
note "6. POST /resurface/decision — record agent decision"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d '{"agent_id":"smoke-noyu-writes","decision_kind":"email_response_priority","options":["respond_now","draft_for_later","ignore"],"chosen":"draft_for_later","why":"user is in deep-work mode; this is non-urgent"}' \
  "$API/resurface/decision")
DECISION_ID=$(python3 -c "
import json
print(json.load(open('/tmp/r.json')).get('decision_id') or '')
")
[ "$HTTP" = "201" ] && [ -n "$DECISION_ID" ] \
  && ok "decision recorded with id=$DECISION_ID" \
  || bad "decision recording failed: HTTP $HTTP"

note "7. Verify observation row written with structured context"
ROW=$("${PSQL[@]}" -t -c "SET app.tenant_id = '$TENANT';
SELECT context::text FROM resurface_observations
 WHERE observation_id = '$DECISION_ID';" \
  2>&1 | grep -v warning | grep "decision" | head -1)
if echo "$ROW" | python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
d = data.get('decision', {})
ok = (
    d.get('kind') == 'email_response_priority' and
    'respond_now' in (d.get('options') or []) and
    d.get('chosen') == 'draft_for_later' and
    d.get('why', '').startswith('user is')
)
print('match' if ok else f'mismatch: {data}')
" 2>/dev/null | grep -q "^match$"; then
  ok "context.decision shape correct"
else
  bad "context shape wrong: $ROW"
fi

note "8. Validation: missing chosen → still records (chosen can be null)"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d '{"agent_id":"smoke-noyu-writes","decision_kind":"test_undecided","options":["a","b"]}' \
  "$API/resurface/decision")
[ "$HTTP" = "201" ] && ok "decision with null chosen accepted" \
  || bad "expected 201, got $HTTP"

note "9. Validation: missing decision_kind → 422"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d '{"agent_id":"smoke-noyu-writes","options":["a"]}' \
  "$API/resurface/decision")
[ "$HTTP" = "422" ] && ok "missing decision_kind → 422" \
  || bad "expected 422, got $HTTP"

# Cleanup
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
UPDATE resurface_user_preferences SET persona_tone = '{}'::jsonb
 WHERE tenant_id = '$TENANT';
DELETE FROM resurface_observations WHERE agent_id = 'smoke-noyu-writes';
" >/dev/null 2>&1 || true

echo
echo "=========================================="
echo "PASS=$PASS  FAIL=$FAIL"
echo "=========================================="
exit $FAIL
