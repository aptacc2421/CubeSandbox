#!/usr/bin/env bash
# Verify CubeAPI webhook delivery on a real CubeSandbox instance.
#
# Prerequisites:
#   1. CubeSandbox one-click deployment is running (CubeAPI, CubeMaster, etc.)
#      See: https://github.com/TencentCloud/CubeSandbox/blob/master/docs/zh/guide/quickstart.md
#   2. /dev/kvm is available
#   3. A template exists (check via: curl http://localhost:3000/templates)
#
# Usage:
#   chmod +x verify-webhook.sh
#   ./verify-webhook.sh
#
# The script will:
#   1. Build the PR's CubeAPI binary with webhook support
#   2. Start the webhook receiver on :9090
#   3. Start CubeAPI with webhook config pointing to the receiver
#   4. Create → Pause → Resume → Delete a sandbox
#   5. Verify all 4 webhook events were received
#   6. Stop all spawned processes
#
# Output: verification result + logs in /tmp/cube-webhook-verify/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CUBEAPI_DIR="$(dirname "$SCRIPT_DIR")"
RECEIVER_DIR="$CUBEAPI_DIR/../examples/webhook-receiver"
WORKDIR="${WEBHOOK_VERIFY_DIR:-/tmp/cube-webhook-verify}"
LOG_DIR="$WORKDIR/logs"
EVIDENCE_DIR="$WORKDIR/evidence"
WEBHOOK_SECRET="verify-secret-$(date +%s)"
RECEIVER_PORT="${WEBHOOK_RECEIVER_PORT:-9090}"
WEBHOOK_URL="http://127.0.0.1:${RECEIVER_PORT}/webhook"

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; }
info() { echo -e "${YELLOW}[INFO]${NC} $*"; }

# ── Setup ────────────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR" "$EVIDENCE_DIR"
info "Working directory: $WORKDIR"
info "Webhook secret: $WEBHOOK_SECRET"

cleanup() {
    info "Cleaning up background processes..."
    [[ -n "${API_PID:-}" ]] && kill "$API_PID" 2>/dev/null || true
    [[ -n "${RECEIVER_PID:-}" ]] && kill "$RECEIVER_PID" 2>/dev/null || true
}
trap cleanup EXIT

# ── Step 1: Build CubeAPI ────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo " Step 1/7: Build CubeAPI with webhook support"
echo "═══════════════════════════════════════════════════════════"

cd "$CUBEAPI_DIR"
cargo build --release 2>&1 | tee "$LOG_DIR/build.log"
BINARY="$(pwd)/target/release/cube-api"
if [[ ! -x "$BINARY" ]]; then
    fail "CubeAPI binary not found at $BINARY"
    exit 1
fi
pass "CubeAPI built successfully"

# ── Step 2: Build receiver ────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo " Step 2/7: Build webhook receiver"
echo "═══════════════════════════════════════════════════════════"

cd "$RECEIVER_DIR"
cargo build --release 2>&1 | tee "$LOG_DIR/receiver-build.log"
RECEIVER_BIN="$(pwd)/target/release/webhook-receiver"
if [[ ! -x "$RECEIVER_BIN" ]]; then
    fail "Receiver binary not found at $RECEIVER_BIN"
    exit 1
fi
pass "Receiver built successfully"

# ── Step 3: Check prerequisites ──────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo " Step 3/7: Check CubeSandbox cluster health"
echo "═══════════════════════════════════════════════════════════"

HEALTH=$(curl -sf http://localhost:3000/health 2>&1 || echo "FAILED")
if [[ "$HEALTH" == "FAILED" ]]; then
    fail "CubeAPI not running on http://localhost:3000. Is CubeSandbox deployed?"
    exit 1
fi
pass "CubeAPI /health: $HEALTH"

# Get a valid template_id
TEMPLATE_ID=$(curl -sf http://localhost:3000/templates 2>/dev/null | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['templateID'] if isinstance(d,list) else d['templateID'])" 2>/dev/null || echo "")
if [[ -z "$TEMPLATE_ID" ]]; then
    fail "No template found. Create one first or set TEMPLATE_ID env var."
    exit 1
fi
pass "Template: $TEMPLATE_ID"

# ── Step 4: Start receiver ───────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo " Step 4/7: Start webhook receiver on :$RECEIVER_PORT"
echo "═══════════════════════════════════════════════════════════"

WEBHOOK_SECRET="$WEBHOOK_SECRET" \
    PORT="$RECEIVER_PORT" \
    LISTEN="127.0.0.1" \
    "$RECEIVER_BIN" > "$LOG_DIR/receiver.log" 2>&1 &
RECEIVER_PID=$!
sleep 2

if ! kill -0 "$RECEIVER_PID" 2>/dev/null; then
    fail "Receiver failed to start. Check $LOG_DIR/receiver.log"
    exit 1
fi
pass "Receiver started (PID $RECEIVER_PID)"

# ── Step 5: Start CubeAPI with webhook ────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo " Step 5/7: Start CubeAPI with webhook config"
echo "═══════════════════════════════════════════════════════════"

# Stop the existing CubeAPI first if managed externally
# Uncomment if needed:
# systemctl stop cube-api 2>/dev/null || true
# sleep 1

CUBE_WEBHOOK_URLS="$WEBHOOK_URL" \
    CUBE_WEBHOOK_SECRET="$WEBHOOK_SECRET" \
    CUBE_WEBHOOK_EVENTS="sandbox.created,sandbox.deleted,sandbox.paused,sandbox.resumed" \
    "$BINARY" > "$LOG_DIR/cubeapi.log" 2>&1 &
API_PID=$!
sleep 3

if ! kill -0 "$API_PID" 2>/dev/null; then
    fail "CubeAPI failed to start. Check $LOG_DIR/cubeapi.log"
    exit 1
fi
# Verify webhook is enabled in logs
if grep -q "webhook logger enabled" "$LOG_DIR/cubeapi.log"; then
    pass "CubeAPI started with webhook enabled (PID $API_PID)"
else
    fail "Webhook logger not enabled! Check $LOG_DIR/cubeapi.log"
    grep -i webhook "$LOG_DIR/cubeapi.log" || true
    exit 1
fi

# ── Step 6: Trigger sandbox lifecycle ────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo " Step 6/7: Trigger sandbox lifecycle events"
echo "═══════════════════════════════════════════════════════════"

info "Creating sandbox..."
CREATE_RESP=$(curl -sf -X POST http://localhost:3000/sandboxes \
    -H "Content-Type: application/json" \
    -d "{\"templateID\": \"$TEMPLATE_ID\"}" 2>&1)
SANDBOX_ID=$(echo "$CREATE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['sandboxID'])" 2>/dev/null || echo "")
if [[ -z "$SANDBOX_ID" ]]; then
    fail "Failed to create sandbox: $CREATE_RESP"
    exit 1
fi
pass "Sandbox created: $SANDBOX_ID"
echo "$CREATE_RESP" > "$EVIDENCE_DIR/create-response.json"

sleep 3  # Wait for webhook delivery

info "Pausing sandbox..."
curl -sf -X POST "http://localhost:3000/sandboxes/$SANDBOX_ID/pause" -o /dev/null
pass "Sandbox paused"

sleep 3

info "Resuming sandbox..."
RESUME_RESP=$(curl -sf -X POST "http://localhost:3000/sandboxes/$SANDBOX_ID/resume" \
    -H "Content-Type: application/json" \
    -d '{"timeout": 300}' 2>&1)
pass "Sandbox resumed"
echo "$RESUME_RESP" > "$EVIDENCE_DIR/resume-response.json"

sleep 3

info "Deleting sandbox..."
curl -sf -X DELETE "http://localhost:3000/sandboxes/$SANDBOX_ID" -o /dev/null
pass "Sandbox deleted"

sleep 3  # Wait for final webhook

# ── Step 7: Verify results ───────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo " Step 7/7: Verify webhook delivery"
echo "═══════════════════════════════════════════════════════════"

# Collect evidence
cp "$LOG_DIR/receiver.log" "$EVIDENCE_DIR/"
cp "$LOG_DIR/cubeapi.log" "$EVIDENCE_DIR/"

echo ""
echo "─── CubeAPI webhook-related logs ───"
grep -i 'webhook\|HttpLogger' "$LOG_DIR/cubeapi.log" | tee "$EVIDENCE_DIR/cubeapi-webhook.log" || true

echo ""
echo "─── Receiver output ───"
cat "$LOG_DIR/receiver.log" | tee "$EVIDENCE_DIR/receiver-output.log"

# Count received events
RECEIVED=$(grep -c "=== Webhook Received ===" "$LOG_DIR/receiver.log" 2>/dev/null || echo 0)
REJECTED=$(grep -c "=== Webhook REJECTED" "$LOG_DIR/receiver.log" 2>/dev/null || echo 0)

echo ""
echo "───────────────────────────────────────────────────────────────"
echo "  Verification Summary"
echo "───────────────────────────────────────────────────────────────"
echo "  Template:       $TEMPLATE_ID"
echo "  Sandbox:        $SANDBOX_ID"
echo "  Events received: $RECEIVED"
echo "  Events rejected: $REJECTED"
echo "  Evidence:        $EVIDENCE_DIR"
echo "───────────────────────────────────────────────────────────────"

PASSED=true
EXPECTED=4  # created, paused, resumed, deleted

if [[ "$RECEIVED" -lt "$EXPECTED" ]]; then
    fail "Expected at least $EXPECTED webhook events, got $RECEIVED"
    PASSED=false
else
    pass "Received $RECEIVED webhook events (>= $EXPECTED)"
fi

if [[ "$REJECTED" -gt 0 ]]; then
    fail "$REJECTED events rejected (HMAC mismatch?)"
    PASSED=false
else
    pass "No rejected events (HMAC verification OK)"
fi

# Check for specific event types
for evt in sandbox.created sandbox.deleted sandbox.paused sandbox.resumed; do
    if grep -q "\"event\".*\"$evt\"" "$LOG_DIR/receiver.log" 2>/dev/null; then
        pass "  Event '$evt' received"
    else
        fail "  Event '$evt' NOT received"
        PASSED=false
    fi
done

echo ""

# ── SHA256 hashes for audit ─────────────────────────────────────────────────
cd "$EVIDENCE_DIR"
sha256sum ./* > SHA256SUMS.txt 2>/dev/null || true
echo "SHA256 checksums saved to $EVIDENCE_DIR/SHA256SUMS.txt"

if $PASSED; then
    echo "🎉 All checks passed! Webhook delivery verified."
    echo ""
    echo "Evidence collected in: $EVIDENCE_DIR"
    echo "  - receiver-output.log   : all received webhook events"
    echo "  - cubeapi-webhook.log   : CubeAPI webhook-related logs"
    echo "  - create-response.json  : sandbox creation response"
    echo "  - resume-response.json  : sandbox resume response"
    echo "  - SHA256SUMS.txt        : evidence file checksums"
    echo ""
    echo "For PR submission, include:"
    echo "  1. This terminal output (screenshot or full log)"
    echo "  2. $EVIDENCE_DIR contents (zip it)"
    echo "  3. cargo test -- logging::http output"
    exit 0
else
    fail "Verification FAILED. Check logs in $WORKDIR"
    exit 1
fi
