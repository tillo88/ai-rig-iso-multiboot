#!/bin/bash
# =============================================================================
# STAGE 1/4 — Driver NVIDIA
# Blacklist nouveau -> installa driver -> reboot. NON chiama nvidia-smi qui:
# il modulo nuovo non e' garantito attivo finche' non si riavvia con
# l'initramfs rigenerato (bug P0 segnalato nell'audit, confermato).
# =============================================================================
set -e
exec >> /var/log/ai-rig-stage-driver.log 2>&1
echo "=== Stage 1 (driver) - $(date) ==="

mkdir -p /var/lib/ai-rig

if [ -f /var/lib/ai-rig/stage-driver-done ]; then
    echo "Stage driver gia' completato, esco."
    exit 0
fi

# Blacklist nouveau PRIMA di installare il driver proprietario
cat > /etc/modprobe.d/blacklist-nouveau.conf << 'EOF'
blacklist nouveau
options nouveau modeset=0
EOF
update-initramfs -u

if [ -f /opt/cache/nvidia-driver.run ]; then
    echo "Installo driver NVIDIA..."
    sh /opt/cache/nvidia-driver.run --silent --dkms --no-opengl-files
    systemctl enable nvidia-persistenced.service 2>/dev/null || echo "(nvidia-persistenced non presente, salto — non blocca nulla)"
else
    echo "!!! /opt/cache/nvidia-driver.run non trovato. Impossibile procedere." >&2
    exit 1
fi

touch /var/lib/ai-rig/stage-driver-done
echo "=== Stage 1 completato - $(date). Riavvio tra 5s per attivare il modulo. ==="
sleep 5
systemctl reboot
