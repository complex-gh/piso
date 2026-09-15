#!/bin/sh
# transparent-egress.sh — host-side NAT for transparent vpc :443 intercept.
#
# DNATs every TCP/443 packet arriving on the piso_vpc bridge (except traffic
# already destined to the vpc subnet — worker API, DNS, gateway) to the
# gateway container's vpc address:8084. The gateway sniffs SNI: ruled hosts
# are MITM'd; unruled hosts are spliced as raw TLS.
#
# Needs root + iptables + docker on the docker-daemon host. Idempotent.
#
# Usage:
#   sudo scripts/transparent-egress.sh install
#   sudo scripts/transparent-egress.sh status
#   sudo scripts/transparent-egress.sh uninstall
set -eu

CHAIN=PISO_TRANSPARENT
GW_NAME="${PISO_GATEWAY_CONTAINER:-piso-gateway}"
NET_NAME="${PISO_VPC_NETWORK:-piso_vpc}"

die() { echo "transparent-egress: $*" >&2; exit 1; }

have_iptables() { command -v iptables >/dev/null 2>&1; }
have_docker() { command -v docker >/dev/null 2>&1; }

gateway_ip() {
    docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{if eq $k "'"$NET_NAME"'"}}{{$v.IPAddress}}{{end}}{{end}}' "$GW_NAME" 2>/dev/null
}

vpc_bridge() {
    # Docker names the linux bridge br-<first 12 of network id>.
    id="$(docker network inspect -f '{{.Id}}' "$NET_NAME" 2>/dev/null)" || return 1
    echo "br-$(echo "$id" | cut -c1-12)"
}

vpc_subnet() {
    docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "$NET_NAME" 2>/dev/null
}

install_rules() {
    gw="$(gateway_ip)"
    [ -n "$gw" ] || die "gateway container $GW_NAME is not on $NET_NAME (is it up?)"
    br="$(vpc_bridge)"
    [ -n "$br" ] || die "could not resolve bridge for $NET_NAME"
    subnet="$(vpc_subnet)"
    [ -n "$subnet" ] || die "could not resolve subnet for $NET_NAME"

    iptables -w -t nat -N "$CHAIN" 2>/dev/null || iptables -w -t nat -F "$CHAIN"
    iptables -w -t nat -A "$CHAIN" -d "$subnet" -j RETURN
    iptables -w -t nat -A "$CHAIN" -p tcp --dport 443 -j DNAT --to-destination "$gw:8084"
    if ! iptables -w -t nat -C PREROUTING -i "$br" -j "$CHAIN" 2>/dev/null; then
        iptables -w -t nat -I PREROUTING -i "$br" -j "$CHAIN"
    fi
    echo "transparent-egress: $br TCP/443 → $gw:8084 (except $subnet)"
    echo "transparent-egress: installed"
}

status() {
    iptables -w -t nat -L "$CHAIN" -n -v --line-numbers 2>/dev/null || echo "chain $CHAIN not present"
}

uninstall_rules() {
    br="$(vpc_bridge 2>/dev/null || true)"
    if [ -n "$br" ]; then
        iptables -w -t nat -D PREROUTING -i "$br" -j "$CHAIN" 2>/dev/null || true
    fi
    # also drop a leftover jump that used no -i (older installs)
    iptables -w -t nat -D PREROUTING -j "$CHAIN" 2>/dev/null || true
    iptables -w -t nat -F "$CHAIN" 2>/dev/null || true
    iptables -w -t nat -X "$CHAIN" 2>/dev/null || true
    echo "transparent-egress: uninstalled"
}

have_iptables || die "iptables not found (must run on the docker host as root)"
have_docker || die "docker not found (must run on the docker host)"
case "${1:-install}" in
    install)   install_rules ;;
    status)    status ;;
    uninstall) uninstall_rules ;;
    refresh)   install_rules ;; # alias: no IP list to refresh
    *) echo "usage: $0 {install|status|uninstall}" >&2; exit 2 ;;
esac
