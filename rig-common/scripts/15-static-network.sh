#!/bin/bash
# =============================================================================
# Sostituisce il netplan DHCP usato durante l'installazione con IP statico
# definitivo, agganciato al MAC address (non al nome interfaccia, che con
# porte onboard + schede aggiuntive puo' variare). Evita il bug "stesso IP
# su piu' interfacce" della v3.7.7.
# =============================================================================
set -e
exec >> /var/log/ai-rig-static-network.log 2>&1
echo "=== Static network - $(date) ==="

mkdir -p /var/lib/ai-rig
if [ -f /var/lib/ai-rig/stage-network-done ]; then
    echo "Gia' configurato, esco."
    exit 0
fi

LAN_MAC="2c:f0:5d:56:08:bc"
if [ "$LAN_MAC" = "AA:BB:CC:DD:EE:FF" ]; then
    echo "!!! LAN_MAC non configurato in config/network.env: mantengo DHCP e riprovo al prossimo boot." >&2
    exit 1
fi

rm -f /etc/netplan/*installer-config*.yaml /etc/netplan/50-cloud-init.yaml 2>/dev/null || true

cat > /etc/netplan/99-ai-rig-static.yaml << EOFNET
network:
  version: 2
  ethernets:
    ai-rig-lan:
      match:
        macaddress: "${LAN_MAC}"
      set-name: ai-rig-lan
      dhcp4: false
      addresses:
        - 192.168.111.177/24
      routes:
        - to: default
          via: 192.168.111.254
      nameservers:
        addresses: [8.8.8.8, 8.8.4.4, 192.168.111.254]
EOFNET
chmod 600 /etc/netplan/99-ai-rig-static.yaml
netplan apply || echo "!!! netplan apply fallito, verifica manualmente" >&2

touch /var/lib/ai-rig/stage-network-done
echo "=== Static network completato - $(date) ==="
