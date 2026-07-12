#!/bin/bash
# =============================================================================
# bench-kv-cache.sh — MISURA, non fidarti dei benchmark upstream (fatti su RTX 3090
# Ampere; tu hai 3 GPU Pascal sm_61, dove Flash Attention e' meno battuta).
#
# Confronta, sul MODELLO E HARDWARE VERI, le combinazioni di cache type.
# Uso: sudo bash swap-llama-flavor.sh beellama  (una volta), poi questo script.
# =============================================================================
set -euo pipefail

ROLE=$(cat /etc/ai-rig/role 2>/dev/null || { echo "Non trovo /etc/ai-rig/role" >&2; exit 1; })
source /etc/ai-rig/gpu.env
source "/etc/ai-rig/${ROLE}.env"

BIN=/opt/llama.cpp/build/bin/llama-bench
[ -x "$BIN" ] || { echo "llama-bench non trovato in $BIN" >&2; exit 1; }

export CUDA_VISIBLE_DEVICES CUDA_DEVICE_ORDER=PCI_BUS_ID

echo "=== Modello: $ROLE_MODEL_PATH"
echo "=== Flavor: ${LLAMA_FLAVOR:-?} | GPU: $GPU_COUNT | split: $TENSOR_SPLIT"
echo

run() {
    local label="$1"; shift
    echo "--- $label ---"
    # -p 512 = prompt processing, -n 128 = generazione. -r 2 = 2 ripetizioni.
    "$BIN" -m "$ROLE_MODEL_PATH" --tensor-split "$TENSOR_SPLIT" -ngl 999 \
        -p 512 -n 128 -r 2 "$@" 2>&1 | tail -n 6
    echo
}

run "baseline f16 (nessuna compressione, nessuna FA)" -fa 0
run "f16 + flash-attn" -fa 1
run "q8_0 (≈2x compressione) + FA" -fa 1 -ctk q8_0 -ctv q8_0

if [ "${LLAMA_FLAVOR:-mainline}" = "beellama" ]; then
    run "turbo4 / turbo3_tcq (≈7.5x) + FA" -fa 1 -ctk turbo4 -ctv turbo3_tcq
    run "turbo2 / turbo2 (max compressione) + FA" -fa 1 -ctk turbo2 -ctv turbo2
fi

cat << 'EOF'
=== Come leggere i risultati ===
- 't/s' su 'pp512' = velocita' di lettura del prompt; su 'tg128' = velocita' di generazione.
- Se 'f16 + flash-attn' e' PIU LENTO di 'baseline f16', FA non conviene sulle tue Pascal:
  in quel caso i cache type turbo (che richiedono FA) ti costano velocita' in cambio
  di contesto. Decidi tu il trade-off — con 7 GPU e VRAM aggregata alta, potrebbe
  comunque valerne la pena per Hermes (contesto lungo) e non per Devin (latenza).
- La compressione KV si vede nel consumo VRAM, non in questo output: controlla
  'nvidia-smi' mentre gira, o alza -c fino a trovare il punto di OOM per ogni tipo.
EOF
