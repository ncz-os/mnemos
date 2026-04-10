#!/usr/bin/env bash
# MNEMOS v2 verification suite — versioning, diff, revert, GRAEAE audit
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
    if [ "$actual" = "$expected" ]; then ok "$label: $field=$actual"
    else fail "$label: $field expected '$expected', got '$actual'"; fi
}

assert_http() {
    local label="$1" expected="$2"; shift 2
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" "$@")
    if [ "$code" = "$expected" ]; then ok "$label: HTTP $code"
    else fail "$label: expected HTTP $expected, got $code"; fi
}

# ─────────────────────────────────────────────────────────────────────────────
header "1. Schema"
# ─────────────────────────────────────────────────────────────────────────────

TABLES=$(sudo -u postgres psql -d mnemos -tAc "SELECT tablename FROM pg_tables WHERE tablename IN ('memory_versions','graeae_audit_log') ORDER BY tablename;")
for t in graeae_audit_log memory_versions; do
    if echo "$TABLES" | grep -q "^${t}$"; then ok "table $t exists"; else fail "table $t MISSING"; fi
done

TRIGGERS=$(sudo -u postgres psql -d mnemos -tAc "SELECT trigger_name FROM information_schema.triggers WHERE event_object_table='memories' ORDER BY trigger_name;")
for tr in trg_memory_version_delete trg_memory_version_insert trg_memory_version_update; do
    if echo "$TRIGGERS" | grep -q "^${tr}$"; then ok "trigger $tr"; else fail "trigger $tr MISSING"; fi
done

BACKFILL=$(sudo -u postgres psql -d mnemos -tAc "SELECT COUNT(*) FROM memory_versions WHERE change_type='create';" | tr -d ' ')
if [ "$BACKFILL" -gt "6000" ] 2>/dev/null; then
    ok "backfill: $BACKFILL existing memories versioned"
else
    fail "backfill: only $BACKFILL rows (expected >6000)"
fi

# ─────────────────────────────────────────────────────────────────────────────
header "2. Create → trigger fires → version 1 exists"
# ─────────────────────────────────────────────────────────────────────────────

CREATE=$(curl -s -w '\n%{http_code}' -X POST "$BASE/memories" \
  -H 'Content-Type: application/json' \
  -d '{"content":"v2 test — initial content","category":"test","source_agent":"verify-v2"}')
C_HTTP=$(echo "$CREATE" | tail -1)
C_JSON=$(echo "$CREATE" | head -1)
if [ "$C_HTTP" = "201" ]; then ok "POST /memories: 201"; else fail "POST /memories: $C_HTTP"; fi
MEM_ID=$(echo "$C_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Trigger should have fired — version 1 should exist
V1_COUNT=$(sudo -u postgres psql -d mnemos -tAc "SELECT COUNT(*) FROM memory_versions WHERE memory_id='$MEM_ID' AND version_num=1 AND change_type='create';" | tr -d ' ')
if [ "$V1_COUNT" = "1" ]; then ok "trigger: INSERT → version 1 created"; else fail "trigger: INSERT did not create version 1 (count=$V1_COUNT)"; fi

# ─────────────────────────────────────────────────────────────────────────────
header "3. GET /memories/{id}/versions"
# ─────────────────────────────────────────────────────────────────────────────

VERSIONS=$(curl -s "$BASE/memories/$MEM_ID/versions")
V_COUNT=$(echo "$VERSIONS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
if [ "$V_COUNT" = "1" ]; then ok "GET versions: 1 version"; else fail "GET versions: expected 1, got $V_COUNT"; fi
V1_TYPE=$(echo "$VERSIONS" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['change_type'])")
if [ "$V1_TYPE" = "create" ]; then ok "version 1 change_type=create"; else fail "version 1 change_type=$V1_TYPE"; fi

# ─────────────────────────────────────────────────────────────────────────────
header "4. Update → trigger fires → version 2 (previous state)"
# ─────────────────────────────────────────────────────────────────────────────

curl -s -X PATCH "$BASE/memories/$MEM_ID" \
  -H 'Content-Type: application/json' \
  -d '{"content":"v2 test — updated content"}' > /dev/null

VERSIONS2=$(curl -s "$BASE/memories/$MEM_ID/versions")
V2_COUNT=$(echo "$VERSIONS2" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
if [ "$V2_COUNT" = "2" ]; then ok "PATCH → trigger → 2 versions"; else fail "PATCH → trigger: expected 2 versions, got $V2_COUNT"; fi

# Version 2 should hold the NEW (post-update) content — trigger stores NEW.* on UPDATE
V2_CONTENT=$(curl -s "$BASE/memories/$MEM_ID/versions/2" | python3 -c "import sys,json; print(json.load(sys.stdin)['content'])")
if [ "$V2_CONTENT" = "v2 test — updated content" ]; then
    ok "version 2 contains updated content"
else
    fail "version 2 content: '$V2_CONTENT'"
fi
V2_TYPE=$(curl -s "$BASE/memories/$MEM_ID/versions/2" | python3 -c "import sys,json; print(json.load(sys.stdin)['change_type'])")
if [ "$V2_TYPE" = "update" ]; then ok "version 2 change_type=update"; else fail "version 2 change_type=$V2_TYPE"; fi

# Trivial update (only updated timestamp) should NOT create a new version
curl -s -X PATCH "$BASE/memories/$MEM_ID" \
  -H 'Content-Type: application/json' \
  -d '{"content":"v2 test — updated content"}' > /dev/null  # same content
V_AFTER=$(curl -s "$BASE/memories/$MEM_ID/versions" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
if [ "$V_AFTER" = "2" ]; then ok "no-op PATCH: no new version created"; else fail "no-op PATCH: expected 2 versions, got $V_AFTER"; fi

# ─────────────────────────────────────────────────────────────────────────────
header "5. Diff endpoint"
# ─────────────────────────────────────────────────────────────────────────────

DIFF=$(curl -s "$BASE/memories/$MEM_ID/diff?from=1&to=2")
DIFF_TEXT=$(echo "$DIFF" | python3 -c "import sys,json; print(json.load(sys.stdin)['diff'])")
if echo "$DIFF_TEXT" | grep -q '^\-.*initial'; then
    ok "diff v1→v2: removal line present"
else
    fail "diff v1→v2: expected '-...initial...' line"
fi
if echo "$DIFF_TEXT" | grep -q '^+.*updated'; then
    ok "diff v1→v2: addition line present"
else
    fail "diff v1→v2: expected '+...updated...' line"
fi

# Diff same version → empty diff
SAME_DIFF=$(curl -s "$BASE/memories/$MEM_ID/diff?from=1&to=1" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['diff']))")
if [ "$SAME_DIFF" = "0" ]; then ok "diff v1→v1: empty (identical)"; else fail "diff v1→v1: expected empty, got $SAME_DIFF chars"; fi

# Bad version → 404
assert_http "diff missing version → 404" "404" "$BASE/memories/$MEM_ID/diff?from=99&to=2"

# ─────────────────────────────────────────────────────────────────────────────
header "6. Revert endpoint"
# ─────────────────────────────────────────────────────────────────────────────

REVERT=$(curl -s -w '\n%{http_code}' -X POST "$BASE/memories/$MEM_ID/revert/1")
R_HTTP=$(echo "$REVERT" | tail -1)
R_JSON=$(echo "$REVERT" | head -1)
if [ "$R_HTTP" = "200" ]; then ok "POST /revert: 200"; else fail "POST /revert: $R_HTTP"; fi
R_CONTENT=$(echo "$R_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['content'])")
if [ "$R_CONTENT" = "v2 test — initial content" ]; then
    ok "revert: content restored to v1"
else
    fail "revert: content='$R_CONTENT'"
fi

# Revert should trigger another version snapshot
V_AFTER_REVERT=$(curl -s "$BASE/memories/$MEM_ID/versions" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
if [ "$V_AFTER_REVERT" = "3" ]; then
    ok "revert: created version 3 (previous state before revert)"
else
    fail "revert: expected 3 versions, got $V_AFTER_REVERT"
fi

# Revert nonexistent version → 404
assert_http "revert missing version → 404" "404" \
  -X POST "$BASE/memories/$MEM_ID/revert/99"

# ─────────────────────────────────────────────────────────────────────────────
header "7. Delete → version snapshot survives"
# ─────────────────────────────────────────────────────────────────────────────

assert_http "DELETE /memories/{id}" "204" -X DELETE "$BASE/memories/$MEM_ID"

DEL_V=$(sudo -u postgres psql -d mnemos -tAc "SELECT MAX(version_num) FROM memory_versions WHERE memory_id='$MEM_ID';" | tr -d ' ')
if [ "$DEL_V" = "4" ]; then
    ok "DELETE → version 4 (delete snapshot) preserved after deletion"
else
    fail "DELETE → expected version 4, got $DEL_V"
fi

DEL_TYPE=$(sudo -u postgres psql -d mnemos -tAc "SELECT change_type FROM memory_versions WHERE memory_id='$MEM_ID' AND version_num=$DEL_V;" | tr -d ' ')
if [ "$DEL_TYPE" = "delete" ]; then
    ok "version $DEL_V change_type=delete"
else
    fail "version $DEL_V change_type=$DEL_TYPE"
fi

# Versions endpoint should still work (memory is gone but history remains)
HIST=$(curl -s "$BASE/memories/$MEM_ID/versions")
H_COUNT=$(echo "$HIST" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
if [ "$H_COUNT" = "4" ]; then
    ok "GET versions after deletion: 4 versions still accessible"
else
    fail "GET versions after deletion: expected 4, got $H_COUNT"
fi

# ─────────────────────────────────────────────────────────────────────────────
header "8. GRAEAE audit log"
# ─────────────────────────────────────────────────────────────────────────────

AUDIT=$(curl -s "$BASE/graeae/audit")
AUDIT_COUNT=$(echo "$AUDIT" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "-1")
ok "GET /graeae/audit: $AUDIT_COUNT entries"

VERIFY=$(curl -s "$BASE/graeae/audit/verify")
V_VALID=$(echo "$VERIFY" | python3 -c "import sys,json; print(json.load(sys.stdin)['valid'])")
V_MSG=$(echo "$VERIFY" | python3 -c "import sys,json; print(json.load(sys.stdin)['message'])")
if [ "$V_VALID" = "True" ]; then ok "audit chain valid: $V_MSG"; else fail "audit chain INVALID: $V_MSG"; fi

# ─────────────────────────────────────────────────────────────────────────────
header "Summary"
# ─────────────────────────────────────────────────────────────────────────────
TOTAL=$((PASS + FAIL))
echo
printf "  Passed: %d / %d\n" "$PASS" "$TOTAL"
printf "  Failed: %d / %d\n" "$FAIL" "$TOTAL"
echo
if [ "$FAIL" -eq "0" ]; then echo "  ALL TESTS PASSED"; exit 0
else echo "  $FAIL TEST(S) FAILED"; exit 1; fi
