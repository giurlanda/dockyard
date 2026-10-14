#!/usr/bin/env bash
# Blocca ai container di progetto (rete projects-net, interfaccia br-projects)
# l'accesso alla LAN dell'ufficio e all'host. Internet resta raggiungibile.
#
# Uso:   sudo ./block-lan.sh
# Eccezioni (es. GitLab self-hosted in LAN):  sudo ALLOW_IPS="192.168.1.50" ./block-lan.sh
#
# Idempotente: si può rilanciare. Va eseguito dopo l'avvio di Docker
# (vedi claude-launcher-firewall.service).
set -euo pipefail

BR="br-projects"
ALLOW_IPS="${ALLOW_IPS:-}"
PRIVATE_NETS="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10"

add() {  # aggiunge la regola in cima alla catena solo se manca
  local chain="$1"; shift
  iptables -C "$chain" "$@" 2>/dev/null || iptables -I "$chain" "$@"
}

# 1. Container -> LAN e altre reti private: rifiutato
for net in $PRIVATE_NETS; do
  add DOCKER-USER -i "$BR" -d "$net" -j REJECT
done

# 2. Eccezioni: inserite dopo, quindi finiscono sopra i REJECT
for ip in $ALLOW_IPS; do
  add DOCKER-USER -i "$BR" -d "$ip" -j RETURN
done

# 3. Container -> host (SSH, altri servizi del mini PC): nuove connessioni scartate
add INPUT -i "$BR" -m conntrack --ctstate NEW -j DROP

echo "Regole applicate per $BR."
iptables -S DOCKER-USER | grep -- "$BR" || true
iptables -S INPUT | grep -- "$BR" || true
