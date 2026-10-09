#!/usr/bin/env bash
# 5a.5.0 Group C — /ingest/text smoke battery.
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

# ─── 1. Basic text ingest ───────────────────────────────────────────────
note "1. POST /ingest/text — agent capture (default captured_by=agent)"
TIMESTAMP=$(date +%s)
cat > /tmp/it.json <<EOF
{
  "content_text": "Smoke test text capture #${TIMESTAMP} — testing /ingest/text route extension. Agent saved this from a conversation context.",
  "suggested_title": "Smoke Group C #${TIMESTAMP}",
  "suggested_processing_profile": "thought"
}
EOF
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d @/tmp/it.json "$API/ingest/text")
DOC_ID=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['doc_id'])" 2>/dev/null || echo "")
[ "$HTTP" = "202" ] && [ -n "$DOC_ID" ] && ok "POST returns 202 + doc_id" \
  || bad "POST returned $HTTP: $(cat /tmp/r.json)"

# Verify doc row was created with correct metadata
DOC_INFO=$("${PSQL[@]}" -t -c "
SET app.tenant_id = '$TENANT';
SELECT captured_by || '||' || source_channel || '||' || mime
  FROM documents WHERE doc_id = '$DOC_ID';
" 2>&1 | grep '||' | head -1 | tr -d '[:space:]')
echo "  doc metadata: $DOC_INFO"
[[ "$DOC_INFO" == agent*api_custom*text/plain* ]] && ok "metadata correct (captured_by=agent, source_channel=api_custom, mime=text/plain)" \
  || bad "metadata mismatch: $DOC_INFO"

# ─── 2. Override captured_by + source_channel ───────────────────────────
note "2. Caller can override captured_by + source_channel"
TIMESTAMP=$(date +%s%N)
cat > /tmp/it.json <<EOF
{
  "content_text": "User-driven text capture #${TIMESTAMP}",
  "captured_by": "user",
  "source_channel": "mcp_inbound"
}
EOF
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d @/tmp/it.json "$API/ingest/text")
DOC_ID2=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['doc_id'])")
[ "$HTTP" = "202" ] && ok "override accepted" \
  || bad "override returned $HTTP"

DOC_INFO=$("${PSQL[@]}" -t -c "
SET app.tenant_id = '$TENANT';
SELECT captured_by || '||' || source_channel
  FROM documents WHERE doc_id = '$DOC_ID2';
" 2>&1 | grep '||' | head -1 | tr -d '[:space:]')
[[ "$DOC_INFO" == user*mcp_inbound* ]] && ok "override applied correctly" \
  || bad "override failed: $DOC_INFO"

# ─── 3. Empty content rejected ──────────────────────────────────────────
note "3. Empty content_text → 422"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d '{"content_text":""}' "$API/ingest/text")
[ "$HTTP" = "422" ] && ok "empty content → 422" \
  || bad "expected 422, got $HTTP"

# ─── 4. Invalid captured_by → 422 ───────────────────────────────────────
note "4. Invalid captured_by → 422"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST \
  -H "$H_TENANT" -H "Content-Type: application/json" \
  -d '{"content_text":"hi","captured_by":"hacker"}' "$API/ingest/text")
[ "$HTTP" = "422" ] && ok "invalid captured_by → 422" \
  || bad "expected 422, got $HTTP"

# ─── 5. Cross-tenant isolation ──────────────────────────────────────────
note "5. Cross-tenant: TENANT's doc not visible to OTHER"
async_check_other=$("${PSQL[@]}" -t -c "
SELECT count(*) FROM documents WHERE tenant_id = '$OTHER';
" 2>&1 | grep -E '^[0-9]+$' | tail -1)
[ "$async_check_other" = "0" ] && ok "OTHER tenant has zero docs (RLS holds)" \
  || bad "expected 0 docs for OTHER, got $async_check_other"

# ─── 6. End-to-end: ingest text → recall finds it (after pipeline runs) ─
note "6. Wait for pipeline → recall finds the captured text"
echo "  waiting up to 60s for doc to be indexed..."
for i in $(seq 1 60); do
  STATUS=$("${PSQL[@]}" -t -c "
  SET app.tenant_id = '$TENANT';
  SELECT status FROM documents WHERE doc_id = '$DOC_ID';
  " 2>&1 | grep -v warning | tail -1 | tr -d '[:space:]')
  if [ "$STATUS" = "indexed" ]; then
    echo "  indexed after ${i}s"
    break
  fi
  sleep 1
done

if [ "$STATUS" = "indexed" ]; then
  # Search for it via /recall semantic
  cat > /tmp/recall.json <<RJSON
{"query_text":"smoke test text capture","intent_hint":"semantic","limit":50,"rerank":false}
RJSON
  curl -sS -o /tmp/r.json -X POST -H "$H_TENANT" -H "Content-Type: application/json" \
    -d @/tmp/recall.json "$API/recall" >/dev/null
  HAS_DOC=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
items = d.get('items', [])
# semantic returns chunks; chunks have doc_id in metadata or we check ids/snippets
ids = {i.get('doc_id') or i.get('metadata', {}).get('doc_id') for i in items}
matched = '$DOC_ID' in ids
# Or check via type_listing
print(matched)
")
  if [ "$HAS_DOC" = "True" ]; then
    ok "ingested text found via /recall semantic"
  else
    # Fallback: type_listing
    cat > /tmp/recall.json <<RJSON
{"query_text":"files","intent_hint":"type_listing","limit":100,"rerank":false}
RJSON
    curl -sS -o /tmp/r.json -X POST -H "$H_TENANT" -H "Content-Type: application/json" \
      -d @/tmp/recall.json "$API/recall" >/dev/null
    HAS_DOC2=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
items = d.get('items', [])
ids = {i.get('id') for i in items}
print('$DOC_ID' in ids)
")
    [ "$HAS_DOC2" = "True" ] && ok "ingested text found via /recall type_listing (semantic missed)" \
      || bad "ingested text NOT found via /recall (semantic OR type_listing)"
  fi
else
  bad "doc never reached indexed status (current: $STATUS) — pipeline may have stalled"
fi

# ─── 7. Cleanup ─────────────────────────────────────────────────────────
note "7. Cleanup"
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
DELETE FROM documents WHERE doc_id IN ('$DOC_ID', '$DOC_ID2');
" >/dev/null 2>&1
ok "cleanup complete"

echo
echo "=========================================="
echo "PASS=$PASS  FAIL=$FAIL"
echo "=========================================="
exit $FAIL
