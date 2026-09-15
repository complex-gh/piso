#!/usr/bin/env bash
# smoke.sh — end-to-end verification of the piso gateway (no Docker needed):
#   secrets API, ingress, worker-API isolation, planning approve, log safety.
# MITM/substitution is covered by Go tests (transparent intercept needs a vpc).
# Expects `make build` first; gateway runs locally on 127.0.0.1:18081-4.
set -euo pipefail
cd "$(dirname "$0")/.."

GW=${GW:-bin/gateway}
WORK=$(mktemp -d)
TARGETS=$(pgrep -f "gateway -state" || true)
[ -n "$TARGETS" ] && kill $TARGETS 2>/dev/null || true
trap 'kill $GW_PID 2>/dev/null || true; rm -rf "$WORK"' EXIT

echo "== starting gateway =="
"$GW" \
  -state "$WORK/state.json" -patterns "$WORK/patterns.json" -log "$WORK/req.jsonl" \
  -ca-cert "$WORK/ca.crt" -ca-key "$WORK/ca.key" \
  -ctrl-listen 127.0.0.1:18081 -worker-listen 127.0.0.1:18083 -ingress-listen 127.0.0.1:18082 \
  -transparent-listen 127.0.0.1:18084 \
  > "$WORK/gw.log" 2>&1 &
GW_PID=$!
sleep 1

API=http://127.0.0.1:18081/api/v1
WAPI=http://127.0.0.1:18083/api/v1
pass=0; fail=0
check() { # check <desc> <cond>
  if eval "$2"; then echo "  ✓ $1"; pass=$((pass+1)); else echo "  ✗ $1"; fail=$((fail+1)); fi
}

for i in $(seq 1 20); do curl -s -o /dev/null http://127.0.0.1:18081/api/v1/health && break; sleep 0.5; done

echo "== 1. health + worker API =="
check "control health" "curl -s $API/health | grep -q '\"ok\":true'"
check "worker health" "curl -s $WAPI/worker/health | grep -q '\"ok\":true'"
HOST_ON_WORKER=$(curl -s -o /dev/null -w '%{http_code}' "$WAPI/secrets" || true)
check "worker API does not expose host secrets" "[ \"$HOST_ON_WORKER\" = 404 ]"

echo "== 2. secrets (summaries only) =="
curl -s -X POST "$API/secrets" -H 'Content-Type: application/json' \
  -d '{"name":"httpbin","placeholder":"piso_httpbin_abc123","value":"sk-REAL-KEY-123456789","allowedHosts":["httpbin.org"]}' >/dev/null
SUM=$(curl -s "$API/secrets")
check "GET secrets has placeholder" "echo '$SUM' | grep -q piso_httpbin_abc123"
check "GET secrets omits real value" "! echo '$SUM' | grep -q sk-REAL-KEY-123456789"

echo "== 3. ingress =="
python3 -m http.server 19999 --bind 127.0.0.1 >/dev/null 2>&1 & SRV=$!
sleep 0.5
curl -s -X POST "$API/routes" -H 'Content-Type: application/json' \
  -d '{"name":"preview","worker":"127.0.0.1","port":19999}' >/dev/null
BODY=$(curl -s -H "Host: preview.piso.local" http://127.0.0.1:18082/ | head -1)
check "ingress routes preview.piso.local → worker" "echo '$BODY' | grep -qi '<!DOCTYPE'"
kill $SRV 2>/dev/null || true

echo "== 4. planning ingress approve =="
python3 -m http.server 19998 --bind 127.0.0.1 >/dev/null 2>&1 & PLAN=$!
sleep 0.5
CTRL_CREATE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/ingress/requests" -H 'Content-Type: application/json' \
  -d '{"kind":"planning","worker":"127.0.0.1","port":19998,"slug":"smoke"}' || true)
check "control plane refuses worker planning create" "[ \"$CTRL_CREATE\" != 200 ] && [ \"$CTRL_CREATE\" != 201 ]"
PEND=$(curl -s -X POST "$WAPI/worker/planning" -H 'Content-Type: application/json' \
  -d '{"kind":"planning","worker":"127.0.0.1","port":19998,"slug":"smoke"}')
ING_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['id'])" <<<"$PEND")
check "planning request is pending" "[ -n \"$ING_ID\" ]"
MISS=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: plan-smoke.piso.local" http://127.0.0.1:18082/ || true)
check "pending plan is not routed yet" "[ \"$MISS\" = 404 ]"
curl -s -X POST "$API/ingress/requests/$ING_ID/approve" >/dev/null
PLAN_BODY=$(curl -s -H "Host: plan-smoke.piso.local" http://127.0.0.1:18082/ | head -1)
check "approve opens plan-smoke.piso.local" "echo '$PLAN_BODY' | grep -qi '<!DOCTYPE'"
kill $PLAN 2>/dev/null || true

echo "== 5. log safety =="
LEAK=$(grep -c "sk-REAL-KEY-123456789" "$WORK/req.jsonl" || true)
check "real value never in request log" "[ \"$LEAK\" = 0 ]"

echo
echo "passed: $pass  failed: $fail"
[ "$fail" = 0 ] || exit 1
echo "SMOKE OK"
