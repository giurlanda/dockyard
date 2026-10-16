#!/usr/bin/env bash
# Dockyard host firewall: Projects reach the internet, but not the LAN, the host or Dockyard.
#
# Traffic from the Projects bridge (dockyard0, created by Dockyard) to a private range is
# rejected, except to the allowed IPs; new connections from the bridge to the host are dropped.
#
#   sudo DOCKYARD_ALLOWED_IPS="192.168.1.50,10.0.5.0/24" ./dockyard-firewall.sh
#
# DOCKYARD_ALLOWED_IPS: addresses or ranges (CIDR) on the LAN that Projects may reach, separated
# by commas or spaces. Empty: none.
#
# Idempotent: run it again after changing DOCKYARD_ALLOWED_IPS and the rules match the new list,
# with nothing duplicated. dockyard-firewall.service runs it whenever Docker starts.
#
# IPv4 only: the Projects network has no IPv6.
set -euo pipefail
set -f  # the allowed IPs are split on spaces, never expanded as file names

BRIDGE=dockyard0
CHAIN=DOCKYARD-PROJECTS
PRIVATE_RANGES="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10"
ALLOWED_IPS="${DOCKYARD_ALLOWED_IPS:-}"
ALLOWED_IPS="${ALLOWED_IPS//,/ }"

for ip in $ALLOWED_IPS; do
  if ! [[ $ip =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$ ]]; then
    echo "DOCKYARD_ALLOWED_IPS must be IPv4 addresses or ranges separated by commas," \
      "e.g. 192.168.1.50,10.0.5.0/24, not '$ip'." >&2
    exit 1
  fi
done

ensure() {  # $1 = chain, rest = rule: inserted at the top of the chain unless already there
  local chain="$1"; shift
  iptables -C "$chain" "$@" 2>/dev/null || iptables -I "$chain" "$@"
}

# 1. Projects -> private ranges, in a chain of their own, rebuilt from scratch on every run so a
#    removed allowed IP is blocked again. Allowed IPs come first. iptables-restore replaces the
#    chain in one step: there is no moment without the rules.
{
  echo "*filter"
  echo ":$CHAIN - [0:0]"
  for ip in $ALLOWED_IPS; do
    echo "-A $CHAIN -d $ip -j RETURN"
  done
  for range in $PRIVATE_RANGES; do
    echo "-A $CHAIN -d $range -j REJECT --reject-with icmp-net-prohibited"
  done
  echo "COMMIT"
} | iptables-restore --noflush
# DOCKER-USER is where Docker lets the host filter forwarded traffic; Docker creates it too.
iptables -N DOCKER-USER 2>/dev/null || true
ensure DOCKER-USER -i "$BRIDGE" -j "$CHAIN"

# 2. Projects -> the host itself (SSH and any other service): new connections dropped.
ensure INPUT -i "$BRIDGE" -m conntrack --ctstate NEW -j DROP

echo "Dockyard firewall applied to $BRIDGE (allowed IPs: ${ALLOWED_IPS:-none})."
