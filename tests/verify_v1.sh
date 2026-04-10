#!/usr/bin/env bash
# MNEMOS v1 verification suite
BASE="http://localhost:5002"
PASS=0
FAIL=0

ok()   { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
header() { echo; echo "=== $1 ==="; }

assert_field() {
    local label="$1" json="$2" field="$3" expected="$4"
    local actual
    actual=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$field','__MISSING__'))" 2>/dev/null || echo "__ERROR__")
    if [ "$actual" = "$expected" ]; then
        ok "$label: $field=$actual"
    else
        fail "$label: $field expected '$expected', got '$actual'"
    fi
}

assert_not_null() {
    local label="$1" json="$2" field="$3"
    local actual
    actual=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); v=d.get('$field'); print('PRESENT' if v is not None and v != '' else 'MISSING')" 2>/dev/null || echo "__ERROR__")
    if [ "$actual" = "PRESENT" ]; then
        ok "$label: $field present"
    else
        fail "$label: $field missing or null"
    fi
}

assert_http() {
    local label="$1" expected="$2"
    shift 2
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" "$@")
    if [ "$code" = "$expected" ]; then
        ok "$label: HTTP $code"
    else
        fail "$label: expected HTTP $expected, got $code"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
header "1. Schema verification (database)"
# ─────────────────────────────────────────────────────────────────────────────

COLS=$(sudo -u postgres psql -d mnemos -tAc "SELECT column_name FROM information_schema.columns WHERE table_name='memories';")
for col in owner_id group_id namespace permission_mode source_model source_provider source_session source_agent; do
    if echo "$COLS" | grep -q "^${col}$"; then
        ok "memories.$col exists"
    else
        fail "memories.$col MISSING"
    fi
done

TABLES=$(sudo -u postgres psql -d mnemos -tAc "SELECT tablename FROM pg_tables WHERE schemaname='public';")
for t in users groups user_groups api_keys; do
    if echo "$TABLES" | grep -q "^${t}$"; then
        ok "table $t exists"
    else
        fail "table $t MISSING"
    fi
done

POLICIES=$(sudo -u postgres psql -d mnemos -tAc "SELECT policyname FROM pg_policies WHERE tablename='memories';")
for p in mnemos_personal_bypass mnemos_owner_select mnemos_owner_insert mnemos_owner_update mnemos_owner_delete mnemos_group_select mnemos_world_select; do
    if echo "$POLICIES" | grep -q "^${p}$"; then
        ok "RLS policy $p"
    else
        fail "RLS policy $p MISSING"
    fi
done

DEFAULT_USER=$(sudo -u postgres psql -d mnemos -tAc "SELECT id FROM users WHERE id='default';" | tr -d ' ')
if [ "$DEFAULT_USER" = "default" ]; then ok "default user seeded"; else fail "default user MISSING"; fi

NULL_OWNERS=$(sudo -u postgres psql -d mnemos -tAc "SELECT COUNT(*) FROM memories WHERE owner_id IS NULL;" | tr -d ' ')
if [ "$NULL_OWNERS" = "0" ]; then ok "backfill: 0 null owner_id"; else fail "backfill: $NULL_OWNERS null owner_id"; fi

NULL_NS=$(sudo -u postgres psql -d mnemos -tAc "SELECT COUNT(*) FROM memories WHERE namespace IS NULL;" | tr -d ' ')
if [ "$NULL_NS" = "0" ]; then ok "backfill: 0 null namespace"; else fail "backfill: $NULL_NS null namespace"; fi

RLS_ON=$(sudo -u postgres psql -d mnemos -tAc "SELECT relrowsecurity FROM pg_class WHERE relname='memories';" | tr -d ' ')
if [ "$RLS_ON" = "f" ]; then ok "RLS: off (personal profile — correct)"; else fail "RLS: expected off for personal profile, got $RLS_ON"; fi

# ─────────────────────────────────────────────────────────────────────────────
header "2. Health and basic list"
# ─────────────────────────────────────────────────────────────────────────────

HEALTH=$(curl -s "$BASE/health")
assert_field "GET /health" "$HEALTH" "status" "healthy"
assert_field "GET /health" "$HEALTH" "database_connected" "True"

LIST=$(curl -s "$BASE/memories?limit=3")
COUNT=$(echo "$LIST" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['count'])" 2>/dev/null || echo "-1")
if [ "$COUNT" -gt "0" ] 2>/dev/null; then ok "GET /memories: count=$COUNT"; else fail "GET /memories: count=$COUNT"; fi

# Existing memories now carry v1 fields
FIRST_ID=$(echo "$LIST" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['memories'][0]['id'])")
GET=$(curl -s "$BASE/memories/$FIRST_ID")
assert_field "existing memory" "$GET" "owner_id" "default"
assert_field "existing memory" "$GET" "namespace" "default"
assert_field "existing memory" "$GET" "permission_mode" "600"

# ─────────────────────────────────────────────────────────────────────────────
header "3. Search"
# ─────────────────────────────────────────────────────────────────────────────

SEARCH=$(curl -s -X POST "$BASE/memories/search" \
  -H 'Content-Type: application/json' \
  -d '{"query":"infrastructure","limit":3}')
SCOUNT=$(echo "$SEARCH" | python3 -c "import sys,json; print(json.load(sys.stdin)['count'])" 2>/dev/null || echo "-1")
if [ "$SCOUNT" -ge "0" ] 2>/dev/null; then ok "POST /memories/search: count=$SCOUNT"; else fail "POST /memories/search: $SCOUNT"; fi

# ─────────────────────────────────────────────────────────────────────────────
header "4. Create with provenance"
# ─────────────────────────────────────────────────────────────────────────────

CREATE=$(curl -s -w '\n%{http_code}' -X POST "$BASE/memories" \
  -H 'Content-Type: application/json' \
  -d '{
    "content": "VERIFY: mnemos v1 provenance test",
    "category": "test",
    "namespace": "verify/test",
    "source_model": "gemma4-consult",
    "source_provider": "ollama",
    "source_session": "sess_verify_001",
    "source_agent": "verify-agent"
  }')
HTTP_CODE=$(echo "$CREATE" | tail -1)
CREATE_JSON=$(echo "$CREATE" | head -1)
if [ "$HTTP_CODE" = "201" ]; then ok "POST /memories: HTTP 201"; else fail "POST /memories: expected 201 got $HTTP_CODE"; fi

VERIFY_ID=$(echo "$CREATE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null || echo "")
assert_field "provenance create" "$CREATE_JSON" "owner_id" "default"
assert_field "provenance create" "$CREATE_JSON" "namespace" "verify/test"
assert_field "provenance create" "$CREATE_JSON" "permission_mode" "600"
assert_field "provenance create" "$CREATE_JSON" "source_model" "gemma4-consult"
assert_field "provenance create" "$CREATE_JSON" "source_provider" "ollama"
assert_field "provenance create" "$CREATE_JSON" "source_session" "sess_verify_001"
assert_field "provenance create" "$CREATE_JSON" "source_agent" "verify-agent"

# ─────────────────────────────────────────────────────────────────────────────
header "5. Bulk create with provenance"
# ─────────────────────────────────────────────────────────────────────────────

BULK=$(curl -s -X POST "$BASE/memories/bulk" \
  -H 'Content-Type: application/json' \
  -d '{
    "memories": [
      {"content": "VERIFY bulk 1", "category": "test", "source_agent": "bulk-verify"},
      {"content": "VERIFY bulk 2", "category": "test", "source_model": "gpt-4o"},
      {"content": "", "category": "test"}
    ]
  }')
BULK_COUNT=$(echo "$BULK" | python3 -c "import sys,json; print(json.load(sys.stdin)['created'])" 2>/dev/null || echo "-1")
BULK_ERRORS=$(echo "$BULK" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['errors']))" 2>/dev/null || echo "-1")
if [ "$BULK_COUNT" = "2" ]; then ok "bulk create: 2 created"; else fail "bulk: expected 2, got $BULK_COUNT"; fi
if [ "$BULK_ERRORS" = "1" ]; then ok "bulk create: 1 error (empty content rejected)"; else fail "bulk: expected 1 error, got $BULK_ERRORS"; fi

# ─────────────────────────────────────────────────────────────────────────────
header "6. Update and delete"
# ─────────────────────────────────────────────────────────────────────────────

if [ -n "$VERIFY_ID" ]; then
    PATCH=$(curl -s -X PATCH "$BASE/memories/$VERIFY_ID" \
      -H 'Content-Type: application/json' \
      -d '{"content":"VERIFY: updated content"}')
    assert_field "PATCH /memories/{id}" "$PATCH" "id" "$VERIFY_ID"
    assert_http "DELETE /memories/{id}" "204" -X DELETE "$BASE/memories/$VERIFY_ID"
    assert_http "GET deleted memory → 404" "404" "$BASE/memories/$VERIFY_ID"
else
    fail "skipped update/delete (no VERIFY_ID)"
fi

# ─────────────────────────────────────────────────────────────────────────────
header "7. Admin — users"
# ─────────────────────────────────────────────────────────────────────────────

USERS=$(curl -s "$BASE/admin/users")
UCOUNT=$(echo "$USERS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "-1")
if [ "$UCOUNT" -ge "1" ]; then ok "GET /admin/users: $UCOUNT user(s)"; else fail "GET /admin/users: $UCOUNT"; fi

NEW_USER=$(curl -s -w '\n%{http_code}' -X POST "$BASE/admin/users" \
  -H 'Content-Type: application/json' \
  -d '{"id":"verify_user","display_name":"Verify User","role":"user"}')
NU_HTTP=$(echo "$NEW_USER" | tail -1)
NU_JSON=$(echo "$NEW_USER" | head -1)
if [ "$NU_HTTP" = "201" ]; then ok "POST /admin/users: 201"; else fail "POST /admin/users: $NU_HTTP"; fi
assert_field "new user" "$NU_JSON" "id" "verify_user"
assert_field "new user" "$NU_JSON" "role" "user"

assert_http "duplicate user → 409" "409" \
  -X POST "$BASE/admin/users" \
  -H 'Content-Type: application/json' \
  -d '{"id":"verify_user","display_name":"Dup","role":"user"}'

assert_http "bad role → 422" "422" \
  -X POST "$BASE/admin/users" \
  -H 'Content-Type: application/json' \
  -d '{"id":"bad_role_user","role":"superadmin"}'

# ─────────────────────────────────────────────────────────────────────────────
header "8. Admin — API keys"
# ─────────────────────────────────────────────────────────────────────────────

KEY_RESP=$(curl -s -w '\n%{http_code}' -X POST "$BASE/admin/users/verify_user/apikeys" \
  -H 'Content-Type: application/json' \
  -d '{"label":"verify-key"}')
KR_HTTP=$(echo "$KEY_RESP" | tail -1)
KR_JSON=$(echo "$KEY_RESP" | head -1)
if [ "$KR_HTTP" = "201" ]; then ok "POST /admin/apikeys: 201"; else fail "POST /admin/apikeys: $KR_HTTP"; fi

KEY_ID=$(echo "$KR_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null || echo "")
RAW_KEY=$(echo "$KR_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('raw_key',''))" 2>/dev/null || echo "")
KEY_PREFIX=$(echo "$KR_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('key_prefix',''))" 2>/dev/null || echo "")

if [ -n "$RAW_KEY" ]; then ok "raw_key returned on create"; else fail "raw_key not returned"; fi
if [ ${#RAW_KEY} -eq 64 ]; then ok "raw_key is 64 hex chars (256-bit)"; else fail "raw_key length ${#RAW_KEY} (expected 64)"; fi

EXPECTED_PREFIX="${RAW_KEY:0:8}"
if [ "$KEY_PREFIX" = "$EXPECTED_PREFIX" ]; then ok "key_prefix matches raw_key[:8]"; else fail "key_prefix mismatch: $KEY_PREFIX vs $EXPECTED_PREFIX"; fi

LIST_KEYS=$(curl -s "$BASE/admin/users/verify_user/apikeys")
LK_COUNT=$(echo "$LIST_KEYS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "-1")
if [ "$LK_COUNT" = "1" ]; then ok "list keys: 1 key for verify_user"; else fail "list keys: expected 1, got $LK_COUNT"; fi

HAS_RAW=$(echo "$LIST_KEYS" | python3 -c "
import sys, json
keys = json.load(sys.stdin)
has = any(k.get('raw_key') is not None for k in keys)
print('YES' if has else 'NO')
")
if [ "$HAS_RAW" = "NO" ]; then ok "list keys: raw_key not in list response"; else fail "list keys: raw_key leaked in list response"; fi

if [ -n "$KEY_ID" ]; then
    assert_http "revoke key: 204" "204" -X DELETE "$BASE/admin/apikeys/$KEY_ID"
    assert_http "revoke already-revoked: 404" "404" -X DELETE "$BASE/admin/apikeys/$KEY_ID"
else
    fail "skipped revoke (no KEY_ID)"
fi

assert_http "apikeys for nonexistent user → 404" "404" \
  "$BASE/admin/users/nonexistent_xyz/apikeys"

# ─────────────────────────────────────────────────────────────────────────────
header "9. Auth mode (temporary enable → test → restore)"
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_PATH="/opt/mnemos/config.toml"

# Create a root key for auth testing
sudo -u postgres psql -d mnemos -c "UPDATE users SET role='root' WHERE id='verify_user';" > /dev/null
KEY_RESP2=$(curl -s -X POST "$BASE/admin/users/verify_user/apikeys" \
  -H 'Content-Type: application/json' \
  -d '{"label":"auth-test"}')
KEY_ID2=$(echo "$KEY_RESP2" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null || echo "")
AUTH_KEY=$(echo "$KEY_RESP2" | python3 -c "import sys,json; print(json.load(sys.stdin).get('raw_key',''))" 2>/dev/null || echo "")

# Enable auth
sudo sed -i 's/^enabled = false$/enabled = true/' "$CONFIG_PATH"
sudo systemctl restart mnemos
sleep 2

CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/memories")
if [ "$CODE" = "401" ]; then ok "auth on: no key → 401"; else fail "auth on: expected 401, got $CODE"; fi

CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/memories" \
  -H "Authorization: Bearer $AUTH_KEY")
if [ "$CODE" = "200" ]; then ok "auth on: valid key → 200"; else fail "auth on: expected 200, got $CODE"; fi

CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/memories" \
  -H "Authorization: Bearer 0000000000000000000000000000000000000000000000000000000000000000")
if [ "$CODE" = "401" ]; then ok "auth on: wrong key → 401"; else fail "auth on: expected 401, got $CODE"; fi

CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/health")
if [ "$CODE" = "200" ]; then ok "auth on: /health still public"; else fail "auth on: /health returned $CODE"; fi

# Create memory as verify_user — owner_id should match
AUTH_CREATE=$(curl -s -X POST "$BASE/memories" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $AUTH_KEY" \
  -d '{"content":"auth test memory","category":"test"}')
AUTH_OWNER=$(echo "$AUTH_CREATE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('owner_id','__MISSING__'))" 2>/dev/null || echo "__ERROR__")
AUTH_MEM_ID=$(echo "$AUTH_CREATE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")
if [ "$AUTH_OWNER" = "verify_user" ]; then ok "auth create: owner_id=verify_user"; else fail "auth create: owner_id expected verify_user, got $AUTH_OWNER"; fi

# Restore
sudo sed -i 's/^enabled = true$/enabled = false/' "$CONFIG_PATH"
sudo systemctl restart mnemos
sleep 2

CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/memories")
if [ "$CODE" = "200" ]; then ok "auth restored: /memories → 200 (no auth)"; else fail "auth restore: expected 200, got $CODE"; fi

# ─────────────────────────────────────────────────────────────────────────────
header "10. Cleanup"
# ─────────────────────────────────────────────────────────────────────────────

[ -n "$AUTH_MEM_ID" ] && curl -s -X DELETE "$BASE/memories/$AUTH_MEM_ID" > /dev/null || true
BULK_IDS=$(sudo -u postgres psql -d mnemos -tAc "SELECT id FROM memories WHERE content LIKE 'VERIFY%';" | tr -d ' ')
for id in $BULK_IDS; do
    [ -n "$id" ] && curl -s -X DELETE "$BASE/memories/$id" > /dev/null || true
done
sudo -u postgres psql -d mnemos -c "DELETE FROM users WHERE id='verify_user';" > /dev/null
ok "test data cleaned up"

# ─────────────────────────────────────────────────────────────────────────────
header "Summary"
# ─────────────────────────────────────────────────────────────────────────────
TOTAL=$((PASS + FAIL))
echo
printf "  Passed: %d / %d\n" "$PASS" "$TOTAL"
printf "  Failed: %d / %d\n" "$FAIL" "$TOTAL"
echo
if [ "$FAIL" -eq "0" ]; then
    echo "  ALL TESTS PASSED"
    exit 0
else
    echo "  $FAIL TEST(S) FAILED — check output above"
    exit 1
fi
