#!/bin/bash
# =============================================================================
# WOL persistente. Il nome interfaccia va FISSATO (non ri-scoperto ogni boot)
# perche' su una singola NIC il nome e' stabile una volta assemblato il rig.
# Se 2c:f0:5d:56:08:bc e' ancora il placeholder, lo script rileva l'interfaccia attiva
# al momento e la usa comunque, ma va ri-verificato a MAC reale impostato.
# =============================================================================
set -e
exec >> /var/log/ai-rig-wol-setup.log 2>&1
echo "=== WOL setup - $(date) ==="

WOL_MAC="2c:f0:5d:56:08:bc"
ETH_IFACE=""

if [ "$WOL_MAC" != "AA:BB:CC:DD:EE:FF" ]; then
    ETH_IFACE=$(ip -o link show | awk -F': ' '{print $2}' | while read -r i; do
        [ "$(cat /sys/class/net/"$i"/address 2>/dev/null)" = "$(echo "$WOL_MAC" | tr 'A-F' 'a-f')" ] && echo "$i" && break
    done)
fi
if [ -z "$ETH_IFACE" ]; then
    ETH_IFACE=$(ip -o link show | awk -F': ' '{print $2}' | grep -E '^eth|^en' | head -n1)
    echo "!!! WOL_MAC non configurato o non trovato: fallback su prima NIC rilevata ($ETH_IFACE). Aggiorna config/network.env e rigenera la ISO." >&2
fi

if [ -z "$ETH_IFACE" ]; then
    echo "!!! Nessuna interfaccia ethernet trovata, salto WOL." >&2
    exit 0
fi

echo "$ETH_IFACE" > /etc/ai-rig/wol-iface

cat > /etc/systemd/system/wol-persistent.service << EOFWOL
[Unit]
Description=Wake-on-LAN Persistent (${ETH_IFACE})
After=network.target
[Service]
Type=oneshot
ExecStart=/sbin/ethtool -s ${ETH_IFACE} wol g
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOFWOL

systemctl daemon-reload
systemctl enable --now wol-persistent.service
echo "=== WOL abilitato su ${ETH_IFACE} - $(date) ==="
