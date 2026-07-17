#!/usr/bin/env bash
# tests/test_github_client.sh — Tests for github_client.sh
#
# Validates allowlist enforcement, rate limiting, attribution, subcommand
# whitelist, and flag rejection. Uses a mock gh binary to avoid real API calls.
#
# Usage: bash tests/test_github_client.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CLIENT="$SCRIPT_DIR/github_client.sh"
TMPDIR="$(mktemp -d)"
PASSED=0
FAILED=0

cleanup() {
    rm -rf "$TMPDIR"
}
trap cleanup EXIT

# ─── Setup mock environment ─────────────────────────────────────────────────

# Mock gh binary — logs args to a file instead of calling GitHub
mkdir -p "$TMPDIR/bin"
cat > "$TMPDIR/bin/gh" << 'MOCK'
#!/usr/bin/env bash
echo "$@" >> "$MOCK_GH_LOG"
echo "https://github.com/mock/issue/1"
MOCK
chmod +x "$TMPDIR/bin/gh"
export PATH="$TMPDIR/bin:$PATH"
export MOCK_GH_LOG="$TMPDIR/gh_calls.log"

# Mock token file
mkdir -p "$TMPDIR/claude"
echo "ghp_mock_token_for_testing" > "$TMPDIR/claude/.github-token"

# Mock allowlist with one repo
echo "leviathan-news/be-benthic" > "$TMPDIR/claude/.github-repos-allowlist"

# Override HOME so the script reads our mock files
export HOME="$TMPDIR"
mkdir -p "$TMPDIR/.claude"
cp "$TMPDIR/claude/.github-token" "$TMPDIR/.claude/.github-token"
cp "$TMPDIR/claude/.github-repos-allowlist" "$TMPDIR/.claude/.github-repos-allowlist"

# ─── Test helpers ────────────────────────────────────────────────────────────

assert_pass() {
    local desc="$1"; shift
    if "$@" > "$TMPDIR/stdout" 2>"$TMPDIR/stderr"; then
        PASSED=$((PASSED + 1))
        echo "  PASS: $desc"
    else
        FAILED=$((FAILED + 1))
        echo "  FAIL: $desc (exit code $?)"
        echo "    stdout: $(cat "$TMPDIR/stdout")"
        echo "    stderr: $(cat "$TMPDIR/stderr")"
    fi
}

assert_fail() {
    local desc="$1"; shift
    if "$@" > "$TMPDIR/stdout" 2>"$TMPDIR/stderr"; then
        FAILED=$((FAILED + 1))
        echo "  FAIL: $desc (expected failure but got success)"
        echo "    stdout: $(cat "$TMPDIR/stdout")"
    else
        PASSED=$((PASSED + 1))
        echo "  PASS: $desc (rejected as expected)"
    fi
}

assert_contains() {
    local desc="$1" file="$2" pattern="$3"
    if grep -q "$pattern" "$file"; then
        PASSED=$((PASSED + 1))
        echo "  PASS: $desc"
    else
        FAILED=$((FAILED + 1))
        echo "  FAIL: $desc (pattern '$pattern' not found in $file)"
        echo "    content: $(cat "$file")"
    fi
}

reset_state() {
    rm -f "$TMPDIR/.claude/.github-rate-limit"
    rm -f "$MOCK_GH_LOG"
}

# ─── Tests ───────────────────────────────────────────────────────────────────

echo "=== Allowlist Tests ==="
reset_state

assert_pass "Operator can create issue on allowlisted repo" \
    "$CLIENT" --operator issue create leviathan-news/be-benthic --title "test" --body "test body"

assert_fail "Reject issue on non-allowlisted repo" \
    "$CLIENT" --operator issue create random-org/random-repo --title "test" --body "test body"

assert_fail "Reject PR on non-allowlisted repo" \
    "$CLIENT" --operator pr create random-org/random-repo --title "t" --body "b" --head "feat" --base "main"

echo ""
echo "=== Subcommand Whitelist Tests ==="
reset_state

assert_fail "Reject unknown subcommand" \
    "$CLIENT" --operator clone leviathan-news/be-benthic

assert_fail "Reject unknown issue action" \
    "$CLIENT" --operator issue delete leviathan-news/be-benthic 1

assert_fail "Reject unknown pr action" \
    "$CLIENT" --operator pr merge leviathan-news/be-benthic 1

echo ""
echo "=== Flag Whitelist Tests ==="
reset_state

assert_fail "Reject unknown flag on issue create" \
    "$CLIENT" --operator issue create leviathan-news/be-benthic --title "t" --body "b" --assignee "hacker"

assert_fail "Reject unknown flag on pr create" \
    "$CLIENT" --operator pr create leviathan-news/be-benthic --title "t" --body "b" --head "f" --base "m" --label "bug"

echo ""
echo "=== Required Args Tests ==="
reset_state

assert_fail "issue create requires --title" \
    "$CLIENT" --operator issue create leviathan-news/be-benthic --body "b"

assert_fail "issue create requires --body" \
    "$CLIENT" --operator issue create leviathan-news/be-benthic --title "t"

assert_fail "pr create requires --head" \
    "$CLIENT" --operator pr create leviathan-news/be-benthic --title "t" --body "b" --base "main"

assert_fail "pr create requires --base" \
    "$CLIENT" --operator pr create leviathan-news/be-benthic --title "t" --body "b" --head "feat"

echo ""
echo "=== Non-Operator Attribution Tests ==="
reset_state

assert_pass "Non-operator can create issue with --user" \
    "$CLIENT" --user testuser issue create leviathan-news/be-benthic --title "test" --body "test body"

# Check that the gh call included attribution in the body
assert_contains "Attribution footer appended to gh call" \
    "$MOCK_GH_LOG" "on behalf of"

assert_fail "Non-operator without --user is rejected" \
    "$CLIENT" issue create leviathan-news/be-benthic --title "test" --body "test body"

echo ""
echo "=== Rate Limit Tests ==="
reset_state
# Temporarily lower the rate limit for the boundary test (production default is 30)
export RATE_LIMIT_MAX=3

assert_pass "Non-operator action 1/3" \
    "$CLIENT" --user ratelimituser issue create leviathan-news/be-benthic --title "t1" --body "b1"

assert_pass "Non-operator action 2/3" \
    "$CLIENT" --user ratelimituser issue create leviathan-news/be-benthic --title "t2" --body "b2"

assert_pass "Non-operator action 3/3" \
    "$CLIENT" --user ratelimituser issue create leviathan-news/be-benthic --title "t3" --body "b3"

assert_fail "Non-operator action 4/3 rejected (rate limit)" \
    "$CLIENT" --user ratelimituser issue create leviathan-news/be-benthic --title "t4" --body "b4"

assert_pass "Operator bypasses rate limit" \
    "$CLIENT" --operator issue create leviathan-news/be-benthic --title "t5" --body "b5"

assert_pass "Different user has own limit" \
    "$CLIENT" --user otheruser issue create leviathan-news/be-benthic --title "t6" --body "b6"

unset RATE_LIMIT_MAX

echo ""
echo "=== Allowlist Management Tests ==="
reset_state

assert_pass "allowlist list shows current repos" \
    "$CLIENT" --operator allowlist list

assert_pass "allowlist add new repo" \
    "$CLIENT" --operator allowlist add new-org/new-repo

assert_pass "Issue on newly added repo succeeds" \
    "$CLIENT" --operator issue create new-org/new-repo --title "test" --body "test"

assert_pass "allowlist add duplicate is idempotent" \
    "$CLIENT" --operator allowlist add new-org/new-repo

assert_fail "allowlist add rejects invalid format" \
    "$CLIENT" --operator allowlist add "not-a-valid-repo"

echo ""
echo "=== Issue Edit Tests (operator-only) ==="
reset_state

assert_pass "Operator can edit issue body" \
    "$CLIENT" --operator issue edit leviathan-news/be-benthic 42 --body "updated body"

assert_fail "Non-operator cannot edit issue body (operator-only)" \
    "$CLIENT" --user someuser issue edit leviathan-news/be-benthic 42 --body "attack body"

assert_fail "Edit on non-allowlisted repo rejected" \
    "$CLIENT" --operator issue edit forbidden/repo 42 --body "test"

assert_fail "Edit requires --body" \
    "$CLIENT" --operator issue edit leviathan-news/be-benthic 42

assert_fail "Edit requires valid issue number" \
    "$CLIENT" --operator issue edit leviathan-news/be-benthic abc --body "test"

echo ""
echo "=== Operator Flag Tests ==="
reset_state

assert_pass "Operator issue create skips rate limit and attribution" \
    "$CLIENT" --operator issue create leviathan-news/be-benthic --title "operator test" --body "no footer"

# Verify no attribution in operator call
if grep -q "on behalf of" "$MOCK_GH_LOG" 2>/dev/null; then
    FAILED=$((FAILED + 1))
    echo "  FAIL: Operator call should NOT have attribution footer"
else
    PASSED=$((PASSED + 1))
    echo "  PASS: Operator call has no attribution footer"
fi

echo ""
echo "=== Results ==="
echo "Passed: $PASSED"
echo "Failed: $FAILED"

if [[ "$FAILED" -gt 0 ]]; then
    exit 1
fi
echo "All tests passed."
