#!/usr/bin/env bash
# Section 9 only: auth mode verification (run standalone)
BASE="http://localhost:5002"
PASS=0
FAIL=0

ok()   { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

wait_for_service() {
    local max=15 i=0
    while [ $i -lt $max ]; do
        CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 1 "$BASE/health" 2>/dev/null || echo "000")
        if [ "$CODE" = "200" ] || [ "$CODE" = "401" ]; then
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    echo "  [WARN] service not ready after ${max}s"
    return 1
}

assert_http() {
    local label="$1" expected="$2"
    shift 2
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$@" 2>/dev/null || echo "000")
    if [ "$code" = "$expected" ]; then
        ok "$label: HTTP $code"
    else
        fail "$label: expected HTTP $expected, got $code"
    fi
}

echo "=== 9. Auth mode test ==="

# Create verify_user and escalate to root
sudo -u postgres psql -d mnemos -c "
  INSERT INTO users (id, display_name, role) VALUES ('verify_user', 'Verify User', 'root')
  ON CONFLICT (id) DO UPDATE SET role='root';" > /dev/null

# Create API key
KEY_RESP=$(curl -s -X POST "$BASE/admin/users/verify_user/apikeys" \
  -H 'Content-Type: application/json' \
  -d '{"label":"auth-test"}')
KEY_ID=$(echo "$KEY_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null || echo "")
AUTH_KEY=$(echo "$KEY_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('raw_key',''))" 2>/dev/null || echo "")

if [ -z "$AUTH_KEY" ]; then
    fail "could not create auth test key"; echo "Key response: $KEY_RESP"; exit 1
fi
ok "created auth test key (prefix: ${AUTH_KEY:0:8})"

# Enable auth
python3 /tmp/toggle_auth.py on
sudo systemctl restart mnemos
wait_for_service

# Test: no key → 401
assert_http "no key → 401" "401" "$BASE/memories"

# Test: valid key → 200
assert_http "valid key → 200" "200" "$BASE/memories" \
  -H "Authorization: Bearer $AUTH_KEY"

# Test: wrong key → 401
assert_http "wrong key → 401" "401" "$BASE/memories" \
  -H "Authorization: Bearer 0000000000000000000000000000000000000000000000000000000000000000"

# Test: /health still public (no auth)
assert_http "/health still public" "200" "$BASE/health"

# Test: create memory — owner_id should = verify_user (not default)
AUTH_CREATE=$(curl -s -X POST "$BASE/memories" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $AUTH_KEY" \
  -d '{"content":"auth test memory","category":"test"}')
AUTH_OWNER=$(echo "$AUTH_CREATE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('owner_id','__MISSING__'))" 2>/dev/null || echo "__ERROR__")
AUTH_MEM_ID=$(echo "$AUTH_CREATE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")
if [ "$AUTH_OWNER" = "verify_user" ]; then
    ok "create memory: owner_id=verify_user"
else
    fail "create memory: owner_id expected verify_user, got $AUTH_OWNER  (response: $AUTH_CREATE)"
fi

# Restore auth off
python3 /tmp/toggle_auth.py off
sudo systemctl restart mnemos
wait_for_service

# Confirm restored
assert_http "auth restored: /memories → 200" "200" "$BASE/memories"

# Cleanup
[ -n "$AUTH_MEM_ID" ] && curl -s -X DELETE "$BASE/memories/$AUTH_MEM_ID" > /dev/null || true
sudo -u postgres psql -d mnemos -c "DELETE FROM users WHERE id='verify_user';" > /dev/null
ok "cleaned up auth test data"

echo
printf "  Passed: %d / %d\n" "$PASS" "$((PASS + FAIL))"
printf "  Failed: %d / %d\n" "$FAIL" "$((PASS + FAIL))"
[ "$FAIL" -eq "0" ] && echo "  AUTH TESTS PASSED" && exit 0 || exit 1
