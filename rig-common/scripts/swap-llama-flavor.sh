#!/bin/bash
# =============================================================================
# swap-llama-flavor.sh <beellama|mainline>
#
# Cambia motore di inferenza SUL DISCO ATTIVO, senza rifare la ISO e senza
# toccare i modelli.
#
# Cosa NON viene toccato:
#   - i .gguf in /opt/models/  (formato identico tra i due, nessuna conversione)
#   - config del ruolo, systemd unit, rete, AutoMem, ComfyUI
#
# Cosa viene gestito:
#   - i binari in /opt/llama.cpp (ricompilati)
#   - i cache-type in /etc/ai-rig/<ruolo>.env (turbo4/turbo3_tcq <-> q8_0)
#   - le sessioni KV salvate (--slot-save-path): quelle scritte con i cache type
#     turbo* NON sono leggibili da mainline. Non le cancello: le sposto sul 4°
#     disco in kv-sessions-stash/<flavor>/, cosi' se torni indietro le ritrovi.
#
# ⚠️ Se hai ri-quantizzato dei pesi in TQ3_1S/TQ4_1S (formati SOLO beellama),
#    quei .gguf non si apriranno con mainline. Questo script non li converte —
#    ti avvisa e basta. Regola: non usare TQ per i pesi se vuoi restare reversibile.
# =============================================================================
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Esegui con sudo." >&2; exit 1; }

TARGET="${1:?Uso: $0 <beellama|mainline>}"
case "$TARGET" in
    beellama)
        REPO="https://github.com/Anbeeld/beellama.cpp.git"
        CMAKE_EXTRA="-DGGML_CUDA_FA=ON -DGGML_CUDA_FA_ALL_QUANTS=ON"
        CACHE_K="turbo4"; CACHE_V="turbo3_tcq"; FA="on" ;;
    mainline)
        REPO="https://github.com/ggerganov/llama.cpp.git"
        CMAKE_EXTRA=""
        CACHE_K="q8_0"; CACHE_V="q8_0"; FA="auto" ;;
    *) echo "Flavor sconosciuto: $TARGET (beellama|mainline)" >&2; exit 1 ;;
esac

ROLE=$(cat /etc/ai-rig/role 2>/dev/null || { echo "Non trovo /etc/ai-rig/role" >&2; exit 1; })
ROLE_ENV="/etc/ai-rig/${ROLE}.env"
CURRENT=$(grep -oP '^LLAMA_FLAVOR=\K.*' "$ROLE_ENV" 2>/dev/null || echo "sconosciuto")
CUDA_ARCHS=$(grep -oP '^CUDA_ARCHS="\K[^"]*' /opt/cache/config/rig.env 2>/dev/null || echo "61;75;86")

echo "Ruolo attivo: $ROLE | flavor attuale: $CURRENT -> richiesto: $TARGET"
[ "$CURRENT" = "$TARGET" ] && { echo "Gia' su $TARGET, niente da fare."; exit 0; }

# Avviso pesi TQ (sola andata)
if [ "$TARGET" = "mainline" ] && grep -qiE 'TQ[34]_1S' /opt/ai-rig/build-info.txt 2>/dev/null; then
    echo "⚠️  build-info menziona formati TQ. Se hai .gguf ri-quantizzati in TQ3_1S/TQ4_1S," >&2
    echo "⚠️  mainline NON li aprira'. Continuo comunque tra 10s (Ctrl+C per annullare)." >&2
    sleep 10
fi

echo ">>> Fermo llama-server@${ROLE}..."
systemctl stop "llama-server@${ROLE}.service" || true

# Stash sessioni KV (incompatibili tra flavor diversi)
KV_DIR="/opt/ai-rig/kv-sessions"
STASH_BASE="/mnt/ai-rig-shared/kv-sessions-stash"
if [ -d "$KV_DIR" ] && [ -n "$(ls -A "$KV_DIR" 2>/dev/null)" ]; then
    if mountpoint -q /mnt/ai-rig-shared; then
        mkdir -p "${STASH_BASE}/${CURRENT}"
        echo ">>> Sposto sessioni KV di '${CURRENT}' in ${STASH_BASE}/${CURRENT}/"
        mv "$KV_DIR"/* "${STASH_BASE}/${CURRENT}/" 2>/dev/null || true
        if [ -d "${STASH_BASE}/${TARGET}" ]; then
            echo ">>> Ripristino sessioni KV precedenti di '${TARGET}'"
            cp -a "${STASH_BASE}/${TARGET}/." "$KV_DIR/" 2>/dev/null || true
        fi
    else
        echo "⚠️  4° disco non montato: le sessioni KV incompatibili verranno lasciate dove sono." >&2
        echo "⚠️  llama-server potrebbe rifiutarsi di ricaricarle. Se serve: rm -rf ${KV_DIR}/*" >&2
    fi
fi

echo ">>> Ricompilo ${TARGET} (puo' richiedere 30-60+ min con beellama)..."
rm -rf /opt/llama.cpp.old
[ -d /opt/llama.cpp ] && mv /opt/llama.cpp /opt/llama.cpp.old
git clone --depth 1 "$REPO" /opt/llama.cpp
cd /opt/llama.cpp
# shellcheck disable=SC2086
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCHS" \
    -DGGML_CUDA_FORCE_MMQ=ON $CMAKE_EXTRA -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j"$(nproc)"

if [ ! -f /opt/llama.cpp/build/bin/llama-server ]; then
    echo "!!! Build fallita: ripristino la precedente." >&2
    rm -rf /opt/llama.cpp
    mv /opt/llama.cpp.old /opt/llama.cpp
    systemctl start "llama-server@${ROLE}.service" || true
    exit 1
fi
rm -rf /opt/llama.cpp.old

echo ">>> Aggiorno ${ROLE_ENV}..."
sed -i "s|^LLAMA_FLAVOR=.*|LLAMA_FLAVOR=${TARGET}|"          "$ROLE_ENV"
sed -i "s|^ROLE_CACHE_TYPE_K=.*|ROLE_CACHE_TYPE_K=${CACHE_K}|" "$ROLE_ENV"
sed -i "s|^ROLE_CACHE_TYPE_V=.*|ROLE_CACHE_TYPE_V=${CACHE_V}|" "$ROLE_ENV"
sed -i "s|^ROLE_FLASH_ATTN=.*|ROLE_FLASH_ATTN=${FA}|"          "$ROLE_ENV"

{ echo "flavor=${TARGET}"; echo "swapped_at=$(date -Iseconds)"; } >> /opt/ai-rig/build-info.txt

echo ">>> Riavvio llama-server@${ROLE}..."
systemctl start "llama-server@${ROLE}.service"
sleep 5
systemctl is-active --quiet "llama-server@${ROLE}.service" \
    && echo "✅ Ora su ${TARGET}. Verifica: /usr/local/bin/90-verify.sh" \
    || { echo "❌ llama-server non parte. Log: journalctl -u llama-server@${ROLE} -n 50" >&2; exit 1; }
