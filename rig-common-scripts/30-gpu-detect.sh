#!/bin/bash
# =============================================================================
# STAGE 3/4 — Rilevamento GPU, ordinamento CUDA_VISIBLE_DEVICES, tensor-split
# Logica presa e ripulita dal first-boot originale v3.7.7. Scrive /etc/ai-rig/gpu.env
# che verra' letto da ogni start-llama-<ruolo>.sh
# =============================================================================
set -e
exec >> /var/log/ai-rig-stage-gpudetect.log 2>&1
echo "=== Stage 3 (gpu-detect) - $(date) ==="

mkdir -p /var/lib/ai-rig /etc/ai-rig
if [ -f /var/lib/ai-rig/stage-gpudetect-done ]; then
    echo "Stage gpu-detect gia' completato, esco (rieseguo comunque il rilevamento live)."
fi

if ! command -v nvidia-smi &> /dev/null; then
    echo "!!! nvidia-smi non trovato, esco." >&2
    exit 1
fi

RAM_TOTAL=$(free -g | awk '/^Mem:/{print $2}')
GPU_LIST=$(nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader,nounits)
GPU_COUNT=$(echo "$GPU_LIST" | wc -l)
echo "RAM: ${RAM_TOTAL} GB | GPU rilevate: $GPU_COUNT"

declare -a GPU_INDICES GPU_NAMES GPU_VRAM
while IFS=',' read -r idx name vram; do
    idx=$(echo "$idx" | xargs); name=$(echo "$name" | xargs); vram=$(echo "$vram" | xargs)
    GPU_INDICES+=("$idx"); GPU_NAMES+=("$name"); GPU_VRAM+=("$vram")
    echo "GPU $idx: $name | VRAM: ${vram}MB"
done <<< "$GPU_LIST"

A2000_FOUND="false"; A2000_INDEX=""; TOTAL_VRAM=0
for i in "${!GPU_INDICES[@]}"; do
    vram_int=$(echo "${GPU_VRAM[$i]}" | cut -d'.' -f1)
    TOTAL_VRAM=$((TOTAL_VRAM + vram_int))
    if [[ "${GPU_NAMES[$i]}" == *"A2000"* ]]; then
        A2000_FOUND="true"; A2000_INDEX="${GPU_INDICES[$i]}"
    fi
done
echo "VRAM totale: ${TOTAL_VRAM} MB (~$((TOTAL_VRAM / 1024)) GB) | A2000: $A2000_FOUND ($A2000_INDEX)"

if [ "$A2000_FOUND" = "true" ]; then
    declare -a ORDERED_INDICES=("$A2000_INDEX")
    declare -a OTHER_GPUS
    for i in "${!GPU_INDICES[@]}"; do
        [ "${GPU_INDICES[$i]}" != "$A2000_INDEX" ] && OTHER_GPUS+=("${GPU_VRAM[$i]}|${GPU_INDICES[$i]}")
    done
    for ((i=0; i<${#OTHER_GPUS[@]}; i++)); do
        for ((j=i+1; j<${#OTHER_GPUS[@]}; j++)); do
            vram_i=$(echo "${OTHER_GPUS[$i]}" | cut -d'|' -f1 | cut -d'.' -f1)
            vram_j=$(echo "${OTHER_GPUS[$j]}" | cut -d'|' -f1 | cut -d'.' -f1)
            [ "$vram_j" -gt "$vram_i" ] && { t="${OTHER_GPUS[$i]}"; OTHER_GPUS[$i]="${OTHER_GPUS[$j]}"; OTHER_GPUS[$j]="$t"; }
        done
    done
    for gpu in "${OTHER_GPUS[@]}"; do ORDERED_INDICES+=("$(echo "$gpu" | cut -d'|' -f2)"); done
    CUDA_ORDER=$(IFS=,; echo "${ORDERED_INDICES[*]}")
else
    CUDA_ORDER=$(IFS=,; echo "${GPU_INDICES[*]}")
fi
echo "Ordine CUDA: $CUDA_ORDER"

if [ "$GPU_COUNT" -eq 7 ] && [ "$A2000_FOUND" = "true" ]; then
    TENSOR_SPLIT="0.14,0.20,0.16,0.16,0.12,0.12,0.10"
    echo "Configurazione IDEALE 7 GPU: $TENSOR_SPLIT"
else
    per_gpu=$(echo "scale=4; 1.0 / $GPU_COUNT" | bc)
    TENSOR_SPLIT=""
    for ((i=0; i<GPU_COUNT; i++)); do TENSOR_SPLIT="${TENSOR_SPLIT}${per_gpu},"; done
    TENSOR_SPLIT="${TENSOR_SPLIT%,}"
    echo "!!! ATTENZIONE: non rilevate 7 GPU con A2000, uso split dinamico: $TENSOR_SPLIT" >&2
fi

cat > /etc/ai-rig/gpu.env << EOFGPU
GPU_COUNT=$GPU_COUNT
TOTAL_VRAM_MB=$TOTAL_VRAM
CUDA_VISIBLE_DEVICES=$CUDA_ORDER
TENSOR_SPLIT=$TENSOR_SPLIT
A2000_FOUND=$A2000_FOUND
A2000_INDEX=$A2000_INDEX
RAM_GB=$RAM_TOTAL
EOFGPU

touch /var/lib/ai-rig/stage-gpudetect-done
echo "=== Stage 3 completato - $(date) ==="
