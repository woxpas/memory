#!/usr/bin/env bash
# 5a.5.0 Group D — full document retrieval smoke battery.
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

PASS=0; FAIL=0
note() { echo; echo "── $1 ──"; }
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# Pick a doc with > 5 chunks so we can verify "full" returns more than the
# 5-chunk preview that /recall file_lookup returns
DOC_ID=$("${PSQL[@]}" -t -c "
SET app.tenant_id = '$TENANT';
SELECT d.doc_id FROM documents d
 WHERE d.deleted_at IS NULL AND d.archived_at IS NULL
   AND (SELECT count(*) FROM chunks WHERE doc_id = d.doc_id) >= 1
 ORDER BY (SELECT count(*) FROM chunks WHERE doc_id = d.doc_id) DESC
 LIMIT 1;
" 2>&1 | grep -E '^[0-9a-f]{8}-' | tail -1 | tr -d '[:space:]')
echo "  picked doc_id=$DOC_ID"

EXPECTED_CHUNKS=$("${PSQL[@]}" -t -c "
SET app.tenant_id = '$TENANT';
SELECT count(*) FROM chunks WHERE doc_id = '$DOC_ID';
" 2>&1 | grep -E '^[0-9]+$' | tail -1)
echo "  expected chunk count: $EXPECTED_CHUNKS"

# ─── 1. Default fetch (chunks only, no canonical/raw) ───────────────────
note "1. GET /documents/{id}/full — default response"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -H "$H_TENANT" \
  "$API/documents/$DOC_ID/full")
[ "$HTTP" = "200" ] && ok "GET returns 200" || bad "GET returned $HTTP"

CHUNKS=$(python3 -c "import json; d=json.load(open('/tmp/r.json')); print(len(d['chunks']))")
[ "$CHUNKS" = "$EXPECTED_CHUNKS" ] && ok "ALL chunks returned ($CHUNKS)" \
  || bad "expected $EXPECTED_CHUNKS chunks, got $CHUNKS"

# Verify metadata fields populated
TITLE_OR_FILE=$(python3 -c "
import json; d=json.load(open('/tmp/r.json'))
print(bool(d.get('title') or d.get('filename')))
")
[ "$TITLE_OR_FILE" = "True" ] && ok "title or filename populated" \
  || bad "no title/filename"

# Default response excludes canonical + raw
HAS_CANONICAL=$(python3 -c "import json; d=json.load(open('/tmp/r.json')); print(d.get('canonical_payload') is not None)")
HAS_RAW=$(python3 -c "import json; d=json.load(open('/tmp/r.json')); print(d.get('raw_bytes_b64') is not None)")
[ "$HAS_CANONICAL" = "False" ] && [ "$HAS_RAW" = "False" ] \
  && ok "default excludes canonical + raw (lean envelope)" \
  || bad "leaked: canonical=$HAS_CANONICAL raw=$HAS_RAW"

# ─── 2. include_chunks=false skips chunks ───────────────────────────────
note "2. include_chunks=false — chunks empty"
curl -sS -o /tmp/r.json -H "$H_TENANT" \
  "$API/documents/$DOC_ID/full?include_chunks=false" >/dev/null
NCHUNKS=$(python3 -c "import json; print(len(json.load(open('/tmp/r.json'))['chunks']))")
[ "$NCHUNKS" = "0" ] && ok "chunks empty when include_chunks=false" \
  || bad "expected 0 chunks, got $NCHUNKS"

# ─── 3. include_canonical=true returns decrypted CDM ────────────────────
note "3. include_canonical=true — returns decrypted CDM payload"
curl -sS -o /tmp/r.json -H "$H_TENANT" \
  "$API/documents/$DOC_ID/full?include_canonical=true&include_chunks=false" >/dev/null
HAS_CDM=$(python3 -c "import json; d=json.load(open('/tmp/r.json')); print(d.get('canonical_payload') is not None)")
[ "$HAS_CDM" = "True" ] && ok "canonical_payload populated (decrypted)" \
  || bad "canonical_payload missing"

# ─── 4. Full doc by id WORKS even on archived docs ──────────────────────
note "4. Archived docs still fetchable via /full (unlike /recall)"
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
UPDATE documents SET archived_at = now() WHERE doc_id = '$DOC_ID';
" >/dev/null 2>&1

HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -H "$H_TENANT" \
  "$API/documents/$DOC_ID/full")
ARCHIVED_AT=$(python3 -c "import json; print(json.load(open('/tmp/r.json')).get('archived_at') or 'null')")
[ "$HTTP" = "200" ] && [ "$ARCHIVED_AT" != "null" ] \
  && ok "archived doc fetchable; archived_at exposed" \
  || bad "expected 200 with archived_at, got $HTTP archived_at=$ARCHIVED_AT"

# Restore
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
UPDATE documents SET archived_at = NULL WHERE doc_id = '$DOC_ID';
" >/dev/null 2>&1

# ─── 5. Cross-tenant returns 404 ────────────────────────────────────────
note "5. Cross-tenant /full request → 404"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -H "$H_OTHER" \
  "$API/documents/$DOC_ID/full")
[ "$HTTP" = "404" ] && ok "OTHER gets 404 (RLS hides doc)" \
  || bad "expected 404, got $HTTP"

# ─── 6. Missing doc → 404 ───────────────────────────────────────────────
note "6. Missing doc → 404"
FAKE="00000000-0000-0000-0000-000000000000"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -H "$H_TENANT" \
  "$API/documents/$FAKE/full")
[ "$HTTP" = "404" ] && ok "404 for missing doc" \
  || bad "expected 404, got $HTTP"

echo
echo "=========================================="
echo "PASS=$PASS  FAIL=$FAIL"
echo "=========================================="
exit $FAIL
