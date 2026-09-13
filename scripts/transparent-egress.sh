#!/bin/sh
# transparent-egress.sh — host-side NAT for CCT-style transparent egress.
#
# Makes the worker's default route lead straight to the internet (Docker
# bridge masquerade, now that compose/gateway.yaml sets piso_vpc to
# non-internal) and DNATs RULED hosts' :443 into the gateway's transparent
# listener so substitution/blocking still works for them.
#
# Needs root + iptables on the HOST (docker daemon host). Idempotent.
#
# Usage:
#   sudo scripts/transparent-egress.sh install     # apply rules
#   sudo scripts/transparent-egress.sh refresh     # re-resolve ruled-host IPs
#   sudo scripts/transparent-egress.sh status
#   sudo scripts/transparent-egress.sh uninstall
#
# Environment:
#   PISO_TRANSPARENT_PORT   host port published for :8084 (default 8084)
#   INTERCEPT_HOSTS         space-separated ruled hosts (default: github.com api.github.com)
set -eu

CHAIN=PISO_TRANSPARENT
PORT="${PISO_TRANSPARENT_PORT:-8084}"
HOSTS="${INTERCEPT_HOSTS:-github.com api.github.com}"

die() { echo "transparent-egress: $*" >&2; exit 1; }

have_iptables() { command -v iptables >/dev/null 2>&1; }
have_getent() { command -v getent >/dev/null 2>&1; }

resolve_ips() { # host -> newline-separated IPv4 addrs
    if have_getent; then
        getent ahostsv4 "$1" 2>/dev/null | awk '{print $1}' | sort -u
    else
        # shell-less fallback: host(1) or /etc/hosts via python
        python3 - "$1" <<'PY'
import socket, sys
try:
    for info in socket.getaddrinfo(sys.argv[1], None, socket.AF_INET):
        print(info[4][0])
except socket.gaierror:
    pass
PY
    fi
}

install_rules() {
    iptables -w -t nat -N "$CHAIN" 2>/dev/null || iptables -w -t nat -F "$CHAIN"
    for h in $HOSTS; do
        ips="$(resolve_ips "$h")"
        [ -n "$ips" ] || { echo "transparent-egress: no A records for $h (skip)"; continue; }
        for ip in $ips; do
            # DNAT (not REDIRECT) so the destination is explicitly loopback,
            # matching docker-proxy's 127.0.0.1:$PORT publish of :8084.
            iptables -w -t nat -A "$CHAIN" -d "$ip/32" -p tcp --dport 443 \
                -j DNAT --to-destination "127.0.0.1:$PORT"
            echo "transparent-egress: intercept $h ($ip:443) -> 127.0.0.1:$PORT"
        done
    done
    iptables -w -t nat -A "$CHAIN" -j RETURN
    # Jump into the chain once (idempotent).
    if ! iptables -w -t nat -C PREROUTING -j "$CHAIN" 2>/dev/null; then
        iptables -w -t nat -I PREROUTING -j "$CHAIN"
    fi
    echo "transparent-egress: installed"
}

refresh_rules() {
    iptables -w -t nat -F "$CHAIN"
    install_rules
}

status() {
    iptables -w -t nat -L "$CHAIN" -n -v --line-numbers 2>/dev/null || echo "chain $CHAIN not present"
}

uninstall_rules() {
    iptables -w -t nat -D PREROUTING -j "$CHAIN" 2>/dev/null || true
    iptables -w -t nat -F "$CHAIN" 2>/dev/null || true
    iptables -w -t nat -X "$CHAIN" 2>/dev/null || true
    echo "transparent-egress: uninstalled"
}

have_iptables || die "iptables not found (must run on the docker host as root)"
case "${1:-install}" in
    install)   install_rules ;;
    refresh)   refresh_rules ;;
    status)    status ;;
    uninstall) uninstall_rules ;;
    *) echo "usage: $0 {install|refresh|status|uninstall}" >&2; exit 2 ;;
esac
