#!/usr/bin/env bash
# 5a.5.0 Group E — conflict list/get/resolve smoke battery.
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

# ─── 1. List open conflicts ─────────────────────────────────────────────
note "1. GET /conflicts (default state=open)"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -H "$H_TENANT" "$API/conflicts")
TOTAL=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['total'])")
[ "$HTTP" = "200" ] && ok "list returned 200, total=$TOTAL" \
  || bad "list returned $HTTP"

# Need at least one conflict to test get/resolve. The live tenant has 2.
[ "$TOTAL" -ge 1 ] && ok "has at least one open conflict to test against" \
  || bad "no open conflicts in fixture; cannot proceed"

CONFLICT_ID=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['items'][0]['conflict_id'])")
echo "  conflict_id=$CONFLICT_ID"

# ─── 2. Filter by severity ──────────────────────────────────────────────
note "2. Filter by severity"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -H "$H_TENANT" \
  "$API/conflicts?severity=high&limit=10")
[ "$HTTP" = "200" ] && ok "severity filter accepted" \
  || bad "severity filter returned $HTTP"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -H "$H_TENANT" \
  "$API/conflicts?severity=bogus")
[ "$HTTP" = "422" ] && ok "invalid severity → 422" \
  || bad "invalid severity returned $HTTP"

# ─── 3. Get single conflict ─────────────────────────────────────────────
note "3. GET /conflicts/{id}"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -H "$H_TENANT" \
  "$API/conflicts/$CONFLICT_ID")
SHAPES=$(python3 -c "import json; print(','.join(json.load(open('/tmp/r.json')).get('suggested_shapes',[])))")
[ "$HTTP" = "200" ] && ok "GET detail returned 200" \
  || bad "GET detail returned $HTTP"
[[ "$SHAPES" == *"pick"* ]] && [[ "$SHAPES" == *"coexistence"* ]] \
  && [[ "$SHAPES" == *"progression"* ]] && [[ "$SHAPES" == *"dismissed"* ]] \
  && ok "all four resolution shapes suggested ($SHAPES)" \
  || bad "missing shapes: $SHAPES"

# ─── 4. Validation: pick without winner/loser → 422 ─────────────────────
note "4. pick without winner/loser → 422"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST -H "$H_TENANT" \
  -H "Content-Type: application/json" -d '{"kind":"pick"}' \
  "$API/conflicts/$CONFLICT_ID/resolve")
[ "$HTTP" = "422" ] && ok "pick missing fields → 422" \
  || bad "expected 422, got $HTTP: $(cat /tmp/r.json)"

# ─── 5. Validation: coexistence without discriminators → 422 ────────────
note "5. coexistence without discriminators → 422"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST -H "$H_TENANT" \
  -H "Content-Type: application/json" -d '{"kind":"coexistence"}' \
  "$API/conflicts/$CONFLICT_ID/resolve")
[ "$HTTP" = "422" ] && ok "coexistence missing discriminators → 422" \
  || bad "expected 422, got $HTTP: $(cat /tmp/r.json)"

# ─── 6. Validation: progression without valid_times → 422 ───────────────
note "6. progression without valid_times → 422"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST -H "$H_TENANT" \
  -H "Content-Type: application/json" -d '{"kind":"progression"}' \
  "$API/conflicts/$CONFLICT_ID/resolve")
[ "$HTTP" = "422" ] && ok "progression missing valid_times → 422" \
  || bad "expected 422, got $HTTP: $(cat /tmp/r.json)"

# ─── 7. Resolve via dismissed (simplest shape; doesn't require fields) ──
note "7. resolve dismissed → 200 + new_state=dismissed"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST -H "$H_TENANT" \
  -H "Content-Type: application/json" \
  -d '{"kind":"dismissed","note":"smoke test resolution"}' \
  "$API/conflicts/$CONFLICT_ID/resolve")
NEW_STATE=$(python3 -c "import json; print(json.load(open('/tmp/r.json'))['new_state'])")
[ "$HTTP" = "200" ] && [ "$NEW_STATE" = "dismissed" ] \
  && ok "dismissed transition (state=$NEW_STATE)" \
  || bad "dismissed returned $HTTP state=$NEW_STATE: $(cat /tmp/r.json)"

# ─── 8. Re-resolve already-resolved conflict → 409 ──────────────────────
note "8. Re-resolve already-resolved conflict → 409"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST -H "$H_TENANT" \
  -H "Content-Type: application/json" \
  -d '{"kind":"dismissed"}' \
  "$API/conflicts/$CONFLICT_ID/resolve")
[ "$HTTP" = "409" ] && ok "re-resolve → 409" \
  || bad "expected 409, got $HTTP: $(cat /tmp/r.json)"

# ─── 9. Resolved conflict no longer in default open list ────────────────
note "9. Resolved conflict excluded from default open list"
curl -sS -o /tmp/r.json -H "$H_TENANT" "$API/conflicts" >/dev/null
INCLUDED=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
ids = [i['conflict_id'] for i in d.get('items',[])]
print('$CONFLICT_ID' in ids)
")
[ "$INCLUDED" = "False" ] && ok "resolved conflict no longer in open list" \
  || bad "resolved conflict still in open list"

# But state=all includes it
curl -sS -o /tmp/r.json -H "$H_TENANT" "$API/conflicts?state=all&limit=200" >/dev/null
INCLUDED_ALL=$(python3 -c "
import json
d = json.load(open('/tmp/r.json'))
ids = [i['conflict_id'] for i in d.get('items',[])]
print('$CONFLICT_ID' in ids)
")
[ "$INCLUDED_ALL" = "True" ] && ok "state=all returns the resolved conflict" \
  || bad "state=all missing the resolved conflict"

# ─── 10. Cross-tenant get → 404 ─────────────────────────────────────────
note "10. Cross-tenant /conflicts/{id} → 404"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -H "$H_OTHER" \
  "$API/conflicts/$CONFLICT_ID")
[ "$HTTP" = "404" ] && ok "OTHER gets 404 (RLS hides conflict)" \
  || bad "expected 404, got $HTTP"

# ─── 11. Cross-tenant resolve → 404 ─────────────────────────────────────
note "11. Cross-tenant resolve → 404"
HTTP=$(curl -sS -o /tmp/r.json -w '%{http_code}' -X POST -H "$H_OTHER" \
  -H "Content-Type: application/json" -d '{"kind":"dismissed"}' \
  "$API/conflicts/$CONFLICT_ID/resolve")
[ "$HTTP" = "404" ] && ok "OTHER resolve → 404" \
  || bad "expected 404, got $HTTP"

# ─── 12. Restore the resolved conflict to open (for repeat smoke runs) ──
note "12. Restore conflict to open (cleanup)"
"${PSQL[@]}" -c "
SET app.tenant_id = '$TENANT';
UPDATE conflicts SET resolved_state = 'open',
                     resolved_value = NULL,
                     resolved_at    = NULL,
                     resolved_by    = NULL
 WHERE conflict_id = '$CONFLICT_ID';
" >/dev/null 2>&1
ok "conflict restored to open for re-runs"

echo
echo "=========================================="
echo "PASS=$PASS  FAIL=$FAIL"
echo "=========================================="
exit $FAIL
