#!/bin/bash
# =============================================================================
# 4° disco condiviso tra i 3 ruoli — dati AutoMem (FalkorDB+Qdrant) e sessioni KV
# salvate. Formatta SOLO se il disco non ha gia' un filesystem (cosi' il primo
# ruolo che boota lo inizializza, i successivi lo trovano gia' pronto e NON lo
# ri-formattano mai — altrimenti cancelleresti la memoria ad ogni cambio ruolo).
# =============================================================================
set -e
exec >> /var/log/ai-rig-stage-shareddisk.log 2>&1
echo "=== Stage shared-disk - $(date) ==="

mkdir -p /var/lib/ai-rig
SERIAL="__SHARED_DISK_SERIAL__"
MOUNT_PATH="__SHARED_MOUNT_PATH__"
# Override post-install: se in futuro aggiorni /opt/cache/config/shared-disk.env
# (es. quando arriva il 4° disco) e riavvii, viene letto qui SENZA bisogno di
# rigenerare o rifare la ISO sui 3 dischi gia' installati.
[ -f /opt/cache/config/shared-disk.env ] && source /opt/cache/config/shared-disk.env

if [ "$SERIAL" = "CHANGEME_SHARED_SERIAL" ]; then
    echo "!!! SHARED_DISK_SERIAL non configurato in config/shared-disk.env. Salto (nessun 4° disco)." >&2
    exit 0
fi

disk=$(lsblk -dno NAME,SERIAL | awk -v s="$SERIAL" '$2==s {print $1}' | head -n1)
if [ -z "$disk" ]; then
    echo "!!! Nessun disco con serial $SERIAL trovato — 4° disco non collegato o non ancora arrivato. Salto." >&2
    exit 0
fi
DEV="/dev/${disk}"
mkdir -p "$MOUNT_PATH"

# --- Logica a 3 stadi, a prova di dati altrui ---
# 1) Esiste gia' una NOSTRA partizione (label ai-rig-shared)? -> monta e basta.
# 2) Il disco contiene QUALSIASI altra cosa (partizioni, filesystem)? -> NON
#    toccare nulla: istruzioni nel log e esci. (Il vecchio check guardava solo
#    il device intero: un FAT32 dentro una partizione risultava "vuoto" e
#    veniva sovrascritto — bug di perdita dati, corretto.)
# 3) Disco davvero vergine -> GPT + ext4 con label.
part=$(blkid -o device -t LABEL=ai-rig-shared 2>/dev/null | grep "^${DEV}" | head -n1)
if [ -n "$part" ]; then
    echo "Partizione ai-rig-shared gia' presente: $part — nessuna formattazione."
elif lsblk -no FSTYPE,PTTYPE "$DEV" 2>/dev/null | grep -q '[^[:space:]]'; then
    echo "!!! $DEV contiene partizioni/filesystem NON nostri (nessuna label ai-rig-shared)." >&2
    echo "!!! Per sicurezza NON formatto niente. Se questo disco va inizializzato:" >&2
    echo "!!!   1) salva altrove i dati che ti servono" >&2
    echo "!!!   2) sudo wipefs -a $DEV" >&2
    echo "!!!   3) riavvia: questo stage lo inizializzera' da solo." >&2
    exit 0
else
    echo "Disco $DEV realmente vuoto: creo GPT + ext4 (prima volta)."
    parted -s "$DEV" mklabel gpt
    parted -s "$DEV" mkpart primary ext4 0% 100%
    partprobe "$DEV"
    sleep 2
    part="${DEV}1"
    [ -e "$part" ] || part="${DEV}p1"
    mkfs.ext4 -F -L ai-rig-shared "$part"
fi

UUID=$(blkid -s UUID -o value "$part" 2>/dev/null)
if [ -z "$UUID" ]; then
    echo "!!! Non riesco a determinare lo UUID della partizione condivisa." >&2
    exit 1
fi

grep -q "$UUID" /etc/fstab 2>/dev/null || \
    echo "UUID=${UUID}  ${MOUNT_PATH}  ext4  defaults,nofail  0  2" >> /etc/fstab

mount -a
mkdir -p "${MOUNT_PATH}/automem/falkordb" "${MOUNT_PATH}/automem/qdrant" "${MOUNT_PATH}/kv-sessions-shared"

echo "=== Shared disk pronto su ${MOUNT_PATH} (UUID $UUID) - $(date) ==="
