#!/bin/bash
# =============================================================================
# AutoMem (FalkorDB + Qdrant + API Flask), dati sul 4° disco condiviso.
# Segue il flusso ufficiale documentato (git clone -> make install -> make dev),
# NON un docker-compose scritto da zero — piu' sicuro contro modifiche future
# del repo upstream.
# =============================================================================
set -e
exec >> /var/log/ai-rig-stage-automem.log 2>&1
echo "=== Stage AutoMem - $(date) ==="

mkdir -p /var/lib/ai-rig
MOUNT_PATH="__SHARED_MOUNT_PATH__"

if [ ! -d "$MOUNT_PATH" ] || ! mountpoint -q "$MOUNT_PATH"; then
    echo "!!! $MOUNT_PATH non montato (4° disco assente/non arrivato). Salto AutoMem." >&2
    exit 0
fi
if ! command -v docker &> /dev/null; then
    echo "!!! docker non installato su questo ruolo. Salto." >&2
    exit 0
fi

mkdir -p "${MOUNT_PATH}/automem/falkordb" "${MOUNT_PATH}/automem/qdrant"

if [ ! -d /opt/automem ]; then
    git clone --depth 1 https://github.com/verygoodplugins/automem.git /opt/automem
fi
cd /opt/automem

# Ubuntu 24.04 ha python3.12 di default -> 'make install' lo trova da solo.
make install

# Override dei volumi per puntare i dati sul disco condiviso invece che in un
# volume Docker anonimo (che sparirebbe cambiando ruolo). ATTENZIONE: i nomi
# servizio 'falkordb'/'qdrant' sono quelli documentati nell'architettura del
# progetto — se 'docker compose config --services' mostra nomi diversi dopo
# un aggiornamento upstream, aggiorna questo file di conseguenza.
cat > docker-compose.override.yml << EOFOVERRIDE
services:
  falkordb:
    volumes:
      - ${MOUNT_PATH}/automem/falkordb:/data
  qdrant:
    volumes:
      - ${MOUNT_PATH}/automem/qdrant:/qdrant/storage
EOFOVERRIDE

echo "Servizi definiti nel compose (verifica falkordb/qdrant siano tra questi):"
docker compose config --services || true

make dev

echo "=== AutoMem avviato (dati su ${MOUNT_PATH}/automem) - $(date) ==="
echo "Verifica: curl http://localhost:8001/health"
