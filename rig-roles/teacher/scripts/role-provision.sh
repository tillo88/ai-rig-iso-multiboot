#!/bin/bash
# =============================================================================
# STAGE 4/4 — Provisioning ruolo: teacher
# =============================================================================
set -e
exec >> /var/log/ai-rig-stage-role.log 2>&1
echo "=== Stage 4 (ruolo teacher) - $(date) ==="

mkdir -p /var/lib/ai-rig /etc/ai-rig /opt/models/teacher /opt/ai-rig/kv-sessions
if [ -f /var/lib/ai-rig/stage-role-done ]; then
    echo "Stage ruolo gia' completato, esco."
    exit 0
fi

echo "teacher" > /etc/ai-rig/role

MODEL_SRC="/opt/cache/models/teacher/Qwen_Qwen3-VL-30B-A3B-Thinking-Q6_K_L.gguf"
MODEL_DST="/opt/models/teacher/Qwen_Qwen3-VL-30B-A3B-Thinking-Q6_K_L.gguf"
if [ -f "$MODEL_SRC" ]; then
    cp "$MODEL_SRC" "$MODEL_DST"
else
    echo "!!! Modello non trovato in $MODEL_SRC — copialo in cache/models/teacher/ e ricostruisci la ISO." >&2
fi

MMPROJ_DST=""
if [ -n "mmproj-Qwen_Qwen3-VL-30B-A3B-Thinking-bf16.gguf" ]; then
    MMPROJ_SRC="/opt/cache/models/teacher/mmproj-Qwen_Qwen3-VL-30B-A3B-Thinking-bf16.gguf"
    MMPROJ_DST="/opt/models/teacher/mmproj-Qwen_Qwen3-VL-30B-A3B-Thinking-bf16.gguf"
    if [ -f "$MMPROJ_SRC" ]; then
        cp "$MMPROJ_SRC" "$MMPROJ_DST"
    else
        echo "!!! mmproj non trovato in $MMPROJ_SRC" >&2
        MMPROJ_DST=""
    fi
fi

cat > "/etc/ai-rig/teacher.env" << EOFENV
ROLE_NAME=teacher
ROLE_MODEL_PATH=${MODEL_DST}
ROLE_MMPROJ_PATH=${MMPROJ_DST}
ROLE_LLAMA_PORT=8080
ROLE_CTX_SIZE=8192
ROLE_TEMP=0.7
ROLE_TOP_P=0.8
ROLE_REPEAT_PENALTY=1.05
ROLE_EXTRA_ARGS="--top-k 20 --min-p 0.0 --slot-save-path /opt/ai-rig/kv-sessions"
# Derivati da LLAMA_FLAVOR (config/rig.env) — riscritti da swap-llama-flavor.sh
LLAMA_FLAVOR=beellama
ROLE_CACHE_TYPE_K=turbo4
ROLE_CACHE_TYPE_V=turbo3_tcq
ROLE_FLASH_ATTN=on
EOFENV

# Python venv dedicato al ruolo (requirements/teacher.txt + requirements/common.txt)
if [ -f /opt/cache/requirements/teacher.txt ]; then
    python3 -m venv "/opt/venv-teacher"
    "/opt/venv-teacher/bin/pip" install --upgrade pip -q
    [ -f /opt/cache/requirements/common.txt ] && "/opt/venv-teacher/bin/pip" install -q -r /opt/cache/requirements/common.txt
    "/opt/venv-teacher/bin/pip" install -q -r /opt/cache/requirements/teacher.txt
    chown -R tillo:tillo "/opt/venv-teacher"
fi

chmod +x /usr/local/bin/start-llama-teacher.sh 2>/dev/null || true
systemctl daemon-reload
systemctl enable "llama-server@teacher.service"
systemctl start "llama-server@teacher.service" || echo "!!! avvio llama-server fallito, controlla journalctl" >&2

touch /var/lib/ai-rig/stage-role-done
echo "=== Stage 4 completato - $(date) ==="

# Verifica finale (best effort, non blocca il boot se fallisce)
/usr/local/bin/90-verify.sh || true
