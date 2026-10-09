#!/usr/bin/env bash
# 5a.5.0 Group B — document archive/unarchive smoke battery.
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

# Pick a doc that's currently NOT archived
DOC_ID=$("${PSQL[@]}" -t -c "
SET app.tenant_id = '$TENANT';
SELECT doc_id FROM documents
 WHERE archived_at IS NULL AND deleted_at IS NULL
 LIMIT 1;
" 2>&1 | grep -E '^[0-9a-f]{8}-' | tail -1 | tr -d '[:space:]')
echo "  picked doc_id=$DOC_ID"

# Make sure it's NOT archived to start (in case prior smoke left it archived)
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
UPDATE documents SET archived_at = NULL WHERE doc_id = '$DOC_ID';
" >/dev/null 2>&1 || true

# ─── 1. Archive ─────────────────────────────────────────────────────────
note "1. POST /documents/{id}/archive"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d '{"reason":"smoke test"}' "$API/documents/$DOC_ID/archive")
CHANGED=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['state_changed'])")
ARCHIVED_AT=$(python3 -c "import json; print(json.load(open('/tmp/r.json')).get('archived_at') or 'null')")
[ "$HTTP" = "200" ] && [ "$CHANGED" = "True" ] && ok "archive transitioned (state_changed=True)" \
  || bad "archive returned $HTTP changed=$CHANGED"
[ "$ARCHIVED_AT" != "null" ] && ok "archived_at populated in response" \
  || bad "archived_at missing"

# ─── 2. Idempotent re-archive ───────────────────────────────────────────
note "2. POST archive again — state_changed=False"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d '{}' "$API/documents/$DOC_ID/archive")
CHANGED=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['state_changed'])")
[ "$HTTP" = "200" ] && [ "$CHANGED" = "False" ] && ok "re-archive idempotent" \
  || bad "re-archive returned $HTTP changed=$CHANGED"

# ─── 3. Archived doc excluded from BOTH retrieval paths ─────────────────
note "3a. Archived doc absent from /recall type_listing"
cat > /tmp/recall.json <<EOF
{"query_text":"files","intent_hint":"type_listing","limit":100,"rerank":false}
EOF
curl -sS -o /tmp/r.json -X POST -H "$H_TENANT" -H "Content-Type: application/json" \
  -d @/tmp/recall.json "$API/recall" >/dev/null
HAS_DOC=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
items = d.get('items', [])
ids = {i.get('id') for i in items}
print('$DOC_ID' in ids)
")
[ "$HAS_DOC" = "False" ] && ok "archived doc not returned by /recall type_listing" \
  || bad "RETRIEVAL LEAK: archived doc still returned"

# Also verify SEMANTIC retrieval excludes archived (vector backend hydrate
# JOIN must filter archived_at — caught + fixed during 5a.5.0 review).
note "3b. Archived doc absent from /recall semantic"
DOC_TITLE=$("${PSQL[@]}" -t -c "
SET app.tenant_id = '$TENANT';
SELECT COALESCE(title, filename, 'document') FROM documents WHERE doc_id = '$DOC_ID';
" 2>&1 | grep -v warning | tail -1 | tr -d '[:space:]')
# Use a token from the title in the semantic query
QUERY_TOKEN=$(echo "$DOC_TITLE" | tr -dc '[:alnum:][:space:]' | awk '{print $1, $2, $3, $4}')
cat > /tmp/recall.json <<EOF
{"query_text":"$QUERY_TOKEN","intent_hint":"semantic","limit":50,"rerank":false}
EOF
curl -sS -o /tmp/r.json -X POST -H "$H_TENANT" -H "Content-Type: application/json" \
  -d @/tmp/recall.json "$API/recall" >/dev/null
HAS_DOC_SEM=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
items = d.get('items', [])
# Semantic returns chunks; doc_id lives in metadata
ids = {i.get('metadata', {}).get('doc_id') for i in items if i.get('metadata')}
print('$DOC_ID' in ids)
")
[ "$HAS_DOC_SEM" = "False" ] && ok "archived doc not returned by /recall semantic (vector backend hydrate filter)" \
  || bad "VECTOR RETRIEVAL LEAK: archived doc still returned via semantic"

# ─── 4. Unarchive ───────────────────────────────────────────────────────
note "4. POST /documents/{id}/unarchive"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" "$API/documents/$DOC_ID/unarchive")
CHANGED=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['state_changed'])")
ARCHIVED_AT=$(python3 -c "import json; print(json.load(open('/tmp/r.json')).get('archived_at') or 'null')")
[ "$HTTP" = "200" ] && [ "$CHANGED" = "True" ] && ok "unarchive transitioned" \
  || bad "unarchive returned $HTTP changed=$CHANGED"
[ "$ARCHIVED_AT" = "null" ] && ok "archived_at cleared" \
  || bad "archived_at not null after unarchive: $ARCHIVED_AT"

# ─── 5. Idempotent re-unarchive ─────────────────────────────────────────
note "5. POST unarchive again — state_changed=False"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" "$API/documents/$DOC_ID/unarchive")
CHANGED=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['state_changed'])")
[ "$HTTP" = "200" ] && [ "$CHANGED" = "False" ] && ok "re-unarchive idempotent" \
  || bad "re-unarchive returned $HTTP changed=$CHANGED"

# ─── 6. Doc visible in /recall after unarchive ──────────────────────────
note "6. Unarchived doc reappears in /recall"
curl -sS -o /tmp/r.json -X POST -H "$H_TENANT" -H "Content-Type: application/json" \
  -d @/tmp/recall.json "$API/recall" >/dev/null
HAS_DOC=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
items = d.get('items', [])
ids = {i.get('id') for i in items}
print('$DOC_ID' in ids)
")
[ "$HAS_DOC" = "True" ] && ok "doc visible again after unarchive" \
  || bad "doc still hidden after unarchive"

# ─── 7. Archive non-existent doc → 404 ─────────────────────────────────
note "7. Archive missing doc → 404"
FAKE="00000000-0000-0000-0000-000000000000"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d '{}' "$API/documents/$FAKE/archive")
[ "$HTTP" = "404" ] && ok "404 for missing doc" \
  || bad "expected 404, got $HTTP"

# ─── 8. Cross-tenant archive blocked ────────────────────────────────────
note "8. Cross-tenant archive — OTHER cannot archive TENANT's doc"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_OTHER" -H "Content-Type: application/json" \
  -d '{}' "$API/documents/$DOC_ID/archive")
# RLS hides the doc from OTHER tenant → engine raises ValueError → 404
[ "$HTTP" = "404" ] && ok "OTHER gets 404 (RLS hides doc)" \
  || bad "expected 404, got $HTTP"

# Verify TENANT's doc is still NOT archived
STATE=$("${PSQL[@]}" -t -c "
SET app.tenant_id = '$TENANT';
SELECT archived_at FROM documents WHERE doc_id = '$DOC_ID';
" 2>&1 | grep -v warning | tail -1 | tr -d '[:space:]')
[ -z "$STATE" ] && ok "TENANT's doc remains unarchived after blocked OTHER attempt" \
  || bad "doc state changed: archived_at=$STATE"

# ─── 9. Cleanup ─────────────────────────────────────────────────────────
note "9. Cleanup"
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
UPDATE documents SET archived_at = NULL WHERE doc_id = '$DOC_ID';
" >/dev/null 2>&1
ok "cleanup complete"

echo
echo "=========================================="
echo "PASS=$PASS  FAIL=$FAIL"
echo "=========================================="
exit $FAIL
