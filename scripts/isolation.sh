#!/usr/bin/env bash
# isolation.sh — Docker-level proof of the transparent vpc:
#   1. Wipe piso containers/networks (testing: no migration).
#   2. compose up the gateway so piso_vpc is created (not internal).
#   3. worker image has no HTTP(S)_PROXY.
#   4. vpc can reach the internet directly (unruled).
#   5. worker API on gateway:8083 is reachable from vpc.
#   6. gateway :8084 is listening on the vpc.
set -euo pipefail
cd "$(dirname "$0")/.."
export PISO_DATA="${PISO_DATA:-$HOME/.piso}"
export PISO_CTRL_PORT="${PISO_CTRL_PORT:-8081}"
export PISO_INGRESS_PORT="${PISO_INGRESS_PORT:-8082}"
mkdir -p "$PISO_DATA"

PROBE_IMAGE=${PROBE_IMAGE:-piso-isolation-probe}
VPC=piso_vpc
EGRESS=piso_egress
GATEWAY=piso-gateway
DIRECT_URL=http://1.1.1.1

pass=0
fail=0
check() { # check <desc> <cond>
  if eval "$2"; then echo "  ✓ $1"; pass=$((pass+1)); else echo "  ✗ $1"; fail=$((fail+1)); fi
}

echo "== resetting piso runtime =="
IDS=$(docker ps -aq --filter name=piso- || true)
if [ -n "$IDS" ]; then
  # shellcheck disable=SC2086
  docker rm -f $IDS >/dev/null
fi
docker network rm "$VPC" "$EGRESS" >/dev/null 2>&1 || true

echo "== starting gateway (creates $VPC) =="
docker compose -f compose/gateway.yaml -p piso up -d --build -t 0
for i in $(seq 1 30); do
  curl -s -o /dev/null http://127.0.0.1:${PISO_CTRL_PORT}/api/v1/health && break
  sleep 1
done
curl -s -o /dev/null http://127.0.0.1:${PISO_CTRL_PORT}/api/v1/health \
  || { echo "gateway control plane did not become healthy"; exit 1; }

INTERNAL=$(docker network inspect "$VPC" --format '{{.Internal}}')
check "$VPC is Internal=false" "[ \"$INTERNAL\" = false ]"

echo "== building probe image $PROBE_IMAGE (alpine + curl) =="
docker build -t "$PROBE_IMAGE" - >/dev/null <<'EOF'
FROM alpine:3.20
RUN apk add --no-cache curl
EOF

echo "== 1. worker image must not bake HTTP(S)_PROXY =="
PROXY_ENV=$(docker image inspect piso-worker --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep -E '^(HTTP|HTTPS)_PROXY=' || true)
check "piso-worker image has no HTTP(S)_PROXY" "[ -z \"$PROXY_ENV\" ]"

echo "== 2. direct egress from $VPC must work (unruled NAT) =="
if docker run --rm --network "$VPC" --name piso-isolation-vpc-direct "$PROBE_IMAGE" \
  curl -sS -o /dev/null --max-time 5 "$DIRECT_URL"; then
  DIRECT_OK=1
else
  DIRECT_OK=0
fi
check "curl $DIRECT_URL from $VPC succeeds" "[ \"$DIRECT_OK\" = 1 ]"

echo "== 3. worker API from $VPC =="
WCODE=$(docker run --rm --network "$VPC" --name piso-isolation-vpc-api "$PROBE_IMAGE" \
  curl -sS -o /dev/null --max-time 5 -w '%{http_code}' "http://${GATEWAY}:8083/api/v1/worker/health" || true)
check "curl gateway:8083 worker health from $VPC returns 200" "[ \"$WCODE\" = 200 ]"

echo "== 4. SNI intercept port from $VPC =="
set +e
docker run --rm --network "$VPC" --name piso-isolation-vpc-sni "$PROBE_IMAGE" \
  curl -sS --max-time 3 -o /dev/null "http://${GATEWAY}:8084/" >/dev/null 2>&1
SNI_RC=$?
set -e
# curl 7 = couldn't connect; 52/56 = connected then non-HTTP reset (listening)
check "gateway:8084 accepts TCP from $VPC" "[ \"$SNI_RC\" != 7 ]"

echo
echo "passed: $pass  failed: $fail"
[ "$fail" = 0 ] || exit 1
echo "ISOLATION OK"
