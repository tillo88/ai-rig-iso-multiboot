#!/bin/bash
# =============================================================================
# STAGE 2/4 — CUDA Toolkit + llama.cpp
# Gira SOLO dopo che stage-driver e' confermato (nvidia-smi deve rispondere,
# altrimenti vuol dire che il reboot dello stage 1 non ha attivato il modulo).
# =============================================================================
set -e
exec >> /var/log/ai-rig-stage-cuda-llama.log 2>&1
echo "=== Stage 2 (cuda+llama) - $(date) ==="

mkdir -p /var/lib/ai-rig
if [ -f /var/lib/ai-rig/stage-cuda-llama-done ]; then
    echo "Stage cuda+llama gia' completato, esco."
    exit 0
fi

if ! nvidia-smi > /dev/null 2>&1; then
    echo "!!! nvidia-smi non risponde. Stage driver non completato correttamente. Interrompo." >&2
    exit 1
fi
nvidia-smi

if [ -f /opt/cache/cuda-toolkit.run ]; then
    echo "Installo CUDA Toolkit..."
    sh /opt/cache/cuda-toolkit.run --toolkit --silent --override
    CUDA_BIN_DIR=$(find /usr/local -maxdepth 1 -name 'cuda-*' | sort -V | tail -n1)
    if [ -n "$CUDA_BIN_DIR" ]; then
        {
            echo "export PATH=${CUDA_BIN_DIR}/bin:\$PATH"
            echo "export LD_LIBRARY_PATH=${CUDA_BIN_DIR}/lib64:\$LD_LIBRARY_PATH"
        } >> /home/tillo/.bashrc
        {
            echo "export PATH=${CUDA_BIN_DIR}/bin:\$PATH"
            echo "export LD_LIBRARY_PATH=${CUDA_BIN_DIR}/lib64:\$LD_LIBRARY_PATH"
        } >> /root/.bashrc
    fi
else
    echo "!!! /opt/cache/cuda-toolkit.run non trovato." >&2
    exit 1
fi

# FIX (audit 2026-07-10): mkdir spostato DENTRO il ramo prebuilt. Prima veniva
# creato /opt/llama.cpp/build/bin incondizionatamente, e nel ramo fallback
# `git clone ... llama.cpp` falliva sempre ("destination path exists and is not
# an empty directory") — il fallback di compilazione non poteva funzionare mai.
if [ -d /opt/cache/llama-prebuilt ] && [ -f /opt/cache/llama-prebuilt/bin/llama-server ]; then
    echo "=== COPIO llama.cpp pre-compilato (build machine ha gia' fatto il lavoro) ==="
    mkdir -p /opt/llama.cpp/build/bin
    cp -r /opt/cache/llama-prebuilt/bin/* /opt/llama.cpp/build/bin/
    chmod +x /opt/llama.cpp/build/bin/*
else
    echo "=== COMPILO beellama sul target (nessuna build pre-compilata in cache) ==="
    [ "beellama" = "beellama" ] && echo "!!! FA_ALL_QUANTS attivo: compilazione LUNGA (30-60+ min)." >&2
    cd /opt
    git clone --depth 1 https://github.com/Anbeeld/beellama.cpp.git llama.cpp
    cd llama.cpp
    cmake -B build \
        -DGGML_CUDA=ON \
        -DCMAKE_CUDA_ARCHITECTURES="61;75;86" \
        -DGGML_CUDA_FORCE_MMQ=ON \
        -DGGML_CUDA_FA=ON -DGGML_CUDA_FA_ALL_QUANTS=ON \
        -DCMAKE_BUILD_TYPE=Release
    cmake --build build --config Release -j"$(nproc)"
fi

# Traccia versione/commit per debug futuro
mkdir -p /opt/ai-rig   # difensivo: a questo stage la dir potrebbe non esistere ancora
{
    echo "flavor=beellama"
    echo "installed_at=$(date -Iseconds)"
    if [ -d /opt/llama.cpp/.git ]; then
        git -C /opt/llama.cpp log -1 --format='commit=%H date=%cI' || true
    else
        echo "source=prebuilt-cache"
    fi
} > /opt/ai-rig/build-info.txt

touch /var/lib/ai-rig/stage-cuda-llama-done
echo "=== Stage 2 completato - $(date) ==="
