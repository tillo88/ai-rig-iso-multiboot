#!/bin/bash
set -e
REPORT="/var/log/ai-rig-verify.log"
ROLE=$(cat /etc/ai-rig/role 2>/dev/null || echo "unknown")
: > "$REPORT"
echo "=== AI Rig Verification (ruolo: $ROLE) - $(date) ===" | tee -a "$REPORT"

if [ -f "/etc/ai-rig/${ROLE}.env" ]; then
    # shellcheck disable=SC1090
    source "/etc/ai-rig/${ROLE}.env"
fi
PORT="${ROLE_LLAMA_PORT:-8080}"

echo "Attendo le API (max 15 min)..." | tee -a "$REPORT"
for ((i=1; i<=180; i++)); do
    if ! systemctl is-active --quiet "llama-server@${ROLE}.service"; then
        echo "❌ llama-server@${ROLE} non attivo/crashato. Interrompo attesa." | tee -a "$REPORT"
        break
    fi
    if curl -fs "http://localhost:${PORT}/health" >/dev/null 2>&1; then
        echo "✅ API attive dopo $((i*5))s." | tee -a "$REPORT"
        break
    fi
    sleep 5
done

echo -e "\n--- llama-server@${ROLE} ---" | tee -a "$REPORT"
systemctl is-active --quiet "llama-server@${ROLE}.service" \
    && echo "✅ ATTIVO" | tee -a "$REPORT" || echo "❌ NON ATTIVO" | tee -a "$REPORT"

HEALTH=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${PORT}/health" 2>/dev/null || echo "000")
echo "API Health: HTTP ${HEALTH}" | tee -a "$REPORT"

echo -e "\n--- GPU ---" | tee -a "$REPORT"
LIVE_GPU_COUNT=""
if command -v nvidia-smi &> /dev/null; then
    nvidia-smi --query-gpu=index,name,memory.total,memory.used,temperature.gpu --format=csv,noheader | tee -a "$REPORT"
    LIVE_GPU_COUNT=$(nvidia-smi --query-gpu=count --format=csv,noheader | head -n1 | xargs)
else
    echo "❌ nvidia-smi non trovato" | tee -a "$REPORT"
fi

echo -e "\n--- Config GPU (/etc/ai-rig/gpu.env) ---" | tee -a "$REPORT"
if [ -f /etc/ai-rig/gpu.env ]; then
    # shellcheck disable=SC1091
    source /etc/ai-rig/gpu.env
    cat /etc/ai-rig/gpu.env | tee -a "$REPORT"
    if [ -n "$LIVE_GPU_COUNT" ]; then
        [ "$LIVE_GPU_COUNT" = "$GPU_COUNT" ] \
            && echo "✅ GPU live ($LIVE_GPU_COUNT) = config ($GPU_COUNT)" | tee -a "$REPORT" \
            || echo "❌ GPU live ($LIVE_GPU_COUNT) DIVERSE da config ($GPU_COUNT) — scheda sparita/non rilevata?" | tee -a "$REPORT"
    fi
else
    echo "❌ gpu.env non trovato (stage 3 non completato?)" | tee -a "$REPORT"
fi

echo -e "\n--- RAM ---" | tee -a "$REPORT"
free -h | tee -a "$REPORT"

echo -e "\n--- Modello (${ROLE}) ---" | tee -a "$REPORT"
ls -lh "/opt/models/${ROLE}/"*.gguf 2>/dev/null | tee -a "$REPORT" || echo "⚠️ Nessun modello trovato in /opt/models/${ROLE}/" | tee -a "$REPORT"

echo -e "\n--- Processo llama-server ---" | tee -a "$REPORT"
if pgrep -fa llama-server > /tmp/llama_proc.txt; then
    cat /tmp/llama_proc.txt | tee -a "$REPORT"
else
    echo "❌ Processo llama-server non trovato" | tee -a "$REPORT"
fi

echo -e "\n--- Tensor Split ---" | tee -a "$REPORT"
if [ -n "${TENSOR_SPLIT:-}" ]; then
    IFS=',' read -ra SPLIT <<< "$TENSOR_SPLIT"
    CHECK_AGAINST="${LIVE_GPU_COUNT:-$GPU_COUNT}"
    [ "${#SPLIT[@]}" -eq "$CHECK_AGAINST" ] \
        && echo "✅ Tensor split coerente (${#SPLIT[@]} valori / $CHECK_AGAINST GPU)" | tee -a "$REPORT" \
        || echo "❌ Tensor split NON coerente (${#SPLIT[@]} valori / $CHECK_AGAINST GPU)" | tee -a "$REPORT"
else
    echo "⚠️ TENSOR_SPLIT non definito" | tee -a "$REPORT"
fi

echo -e "\n=== VERIFICA COMPLETATA — report in $REPORT ===" | tee -a "$REPORT"
