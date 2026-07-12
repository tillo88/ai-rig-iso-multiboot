#!/bin/bash
# =============================================================================
# Backup periodico dello STATO (non dei modelli — quelli sono riproducibili da
# cache/, inutile e ingombrante copiarli) sul 4° disco condiviso, con
# rotazione: tiene solo le ultime KEEP_N copie per ruolo.
# NON gira se il 4° disco non e' montato (nessun errore, esce silenzioso).
# =============================================================================
set -e
exec >> /var/log/ai-rig-backup.log 2>&1
echo "=== Backup - $(date) ==="

MOUNT_PATH="__SHARED_MOUNT_PATH__"
[ -f /opt/cache/config/shared-disk.env ] && source /opt/cache/config/shared-disk.env
KEEP_N=2

if [ ! -d "$MOUNT_PATH" ] || ! mountpoint -q "$MOUNT_PATH"; then
    echo "4° disco non montato, salto."
    exit 0
fi

ROLE=$(cat /etc/ai-rig/role 2>/dev/null || echo "unknown")
BACKUP_DIR="${MOUNT_PATH}/backups/${ROLE}"
mkdir -p "$BACKUP_DIR"

TS=$(date +%Y%m%d-%H%M%S)
ARCHIVE="${BACKUP_DIR}/${ROLE}-${TS}.tar.gz"

# Cosa backuppiamo: config/stato, MAI i modelli .gguf (riproducibili da cache/)
tar -czf "$ARCHIVE" \
    --ignore-failed-read \
    /etc/ai-rig \
    /etc/systemd/system/ai-rig-*.service \
    /etc/netplan/99-ai-rig-static.yaml \
    /opt/ai-rig/build-info.txt \
    $([ -d /home/tillo/.hermes ] && echo /home/tillo/.hermes) \
    $([ -f /opt/comfyui/extra_model_paths.yaml ] && echo /opt/comfyui/extra_model_paths.yaml) \
    2>/dev/null || true

if [ -s "$ARCHIVE" ]; then
    echo "Backup creato: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
else
    echo "!!! Backup vuoto o fallito" >&2
    rm -f "$ARCHIVE"
    exit 1
fi

# Rotazione: tieni solo le ultime KEEP_N per questo ruolo
cd "$BACKUP_DIR"
ls -1t "${ROLE}"-*.tar.gz 2>/dev/null | tail -n +$((KEEP_N + 1)) | while read -r old; do
    echo "Rimuovo backup vecchio: $old"
    rm -f "$old"
done

echo "=== Backup completato - $(date) ==="
