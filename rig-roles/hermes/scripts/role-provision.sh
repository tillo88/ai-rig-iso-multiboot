#!/bin/bash
# =============================================================================
# STAGE 4/4 — Provisioning ruolo: hermes
# =============================================================================
set -e
exec >> /var/log/ai-rig-stage-role.log 2>&1
echo "=== Stage 4 (ruolo hermes) - $(date) ==="

mkdir -p /var/lib/ai-rig /etc/ai-rig /opt/models/hermes /opt/ai-rig/kv-sessions
if [ -f /var/lib/ai-rig/stage-role-done ]; then
    echo "Stage ruolo gia' completato, esco."
    exit 0
fi

echo "hermes" > /etc/ai-rig/role

MODEL_SRC="/opt/cache/models/hermes/Qwen3.6-40B-Deck-Opus-NEO-CODE-HERE-2T-OT-Q6_K.gguf"
MODEL_DST="/opt/models/hermes/Qwen3.6-40B-Deck-Opus-NEO-CODE-HERE-2T-OT-Q6_K.gguf"
if [ -f "$MODEL_SRC" ]; then
    cp "$MODEL_SRC" "$MODEL_DST"
else
    echo "!!! Modello non trovato in $MODEL_SRC — copialo in cache/models/hermes/ e ricostruisci la ISO." >&2
fi

MMPROJ_DST=""
if [ -n "mmproj-F32.gguf" ]; then
    MMPROJ_SRC="/opt/cache/models/hermes/mmproj-F32.gguf"
    MMPROJ_DST="/opt/models/hermes/mmproj-F32.gguf"
    if [ -f "$MMPROJ_SRC" ]; then
        cp "$MMPROJ_SRC" "$MMPROJ_DST"
    else
        echo "!!! mmproj non trovato in $MMPROJ_SRC" >&2
        MMPROJ_DST=""
    fi
fi

cat > "/etc/ai-rig/hermes.env" << EOFENV
ROLE_NAME=hermes
ROLE_MODEL_PATH=${MODEL_DST}
ROLE_MMPROJ_PATH=${MMPROJ_DST}
ROLE_LLAMA_PORT=8080
ROLE_CTX_SIZE=16384
ROLE_TEMP=1.0
ROLE_TOP_P=0.95
ROLE_REPEAT_PENALTY=1.0
ROLE_EXTRA_ARGS="--top-k 20 --min-p 0.0 --presence-penalty 0.0 --slot-save-path /opt/ai-rig/kv-sessions"
# Derivati da LLAMA_FLAVOR (config/rig.env) — riscritti da swap-llama-flavor.sh
LLAMA_FLAVOR=beellama
ROLE_CACHE_TYPE_K=turbo4
ROLE_CACHE_TYPE_V=turbo3_tcq
ROLE_FLASH_ATTN=on
EOFENV

# Python venv dedicato al ruolo (requirements/hermes.txt + requirements/common.txt)
if [ -f /opt/cache/requirements/hermes.txt ]; then
    python3 -m venv "/opt/venv-hermes"
    "/opt/venv-hermes/bin/pip" install --upgrade pip -q
    [ -f /opt/cache/requirements/common.txt ] && "/opt/venv-hermes/bin/pip" install -q -r /opt/cache/requirements/common.txt
    "/opt/venv-hermes/bin/pip" install -q -r /opt/cache/requirements/hermes.txt
    chown -R tillo:tillo "/opt/venv-hermes"
fi

chmod +x /usr/local/bin/start-llama-hermes.sh 2>/dev/null || true
systemctl daemon-reload
systemctl enable "llama-server@hermes.service"
systemctl start "llama-server@hermes.service" || echo "!!! avvio llama-server fallito, controlla journalctl" >&2

touch /var/lib/ai-rig/stage-role-done
echo "=== Stage 4 completato - $(date) ==="

# Verifica finale (best effort, non blocca il boot se fallisce)
/usr/local/bin/90-verify.sh || true
