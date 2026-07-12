#!/bin/bash
# =============================================================================
# Gira SUL RASPBERRY PI. ALTERNATIVA CLI al bot Telegram (che fa la stessa cosa con
# i comandi /devin /hermes /teacher). Utile per cron/script; per l'uso quotidiano
# usa il bot.
# Uso: pi-remote-boot-select.sh <devin|hermes|teacher>
#
# FASE A (questa) - robusta, zero moduli GRUB di rete, zero Secure Boot da toccare:
#   1. Il rig, se spento, ha SEMPRE un ruolo di default che boota da WOL puro
#      (GRUB_DEFAULT=saved, l'ultimo salvato). Consigliato: lascia che sia
#      sempre HERMES il default "a riposo".
#   2. Se vuoi un ruolo diverso da quello di default: manda WOL, aspetta che il
#      default risponda su SSH, poi entra e fai grub-reboot verso il target
#      seguito da un reboot. Un solo giro extra, ma zero tastiera/monitor.
#   3. Se il rig e' GIA' ACCESO nel ruolo giusto, non fa nulla.
#   4. Se il rig e' GIA' ACCESO in un ruolo diverso, fa solo grub-reboot+reboot
#      (nessun bisogno di WOL).
#
# Richiede: chiave SSH del bot gia' autorizzata su tutti e 3 gli utenti tillo
# (vedi cache/ai-rig-bot.pub nel progetto ISO), wakeonlan installato sul Pi.
# =============================================================================
set -euo pipefail

TARGET="${1:?Uso: $0 <devin|hermes|teacher>}"
RIG_MAC="AA:BB:CC:DD:EE:FF"          # = WOL_MAC in config/network.env
RIG_IP="192.168.1.100"
RIG_USER="tillo"
SSH_OPTS="-o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -i /home/pi/.ssh/ai_rig"

# Usa gli --id FISSI creati da scripts/grub-stable-entries.sh (devin/hermes/teacher),
# non i titoli testuali generati da os-prober (cambiano ad ogni update del kernel).
grub_entry_name() {
    case "$1" in
        devin|hermes|teacher) echo "$1" ;;
        *) echo "ERRORE: ruolo sconosciuto '$1'" >&2; exit 1 ;;
    esac
}

is_up() { ssh $SSH_OPTS "${RIG_USER}@${RIG_IP}" true 2>/dev/null; }
current_role() { ssh $SSH_OPTS "${RIG_USER}@${RIG_IP}" cat /etc/ai-rig/role 2>/dev/null; }

ENTRY_NAME=$(grub_entry_name "$TARGET")

if ! is_up; then
    echo "Rig spento. Invio pacchetto WOL..."
    wakeonlan "$RIG_MAC"
    echo "Attendo il boot del ruolo di default (max 3 min)..."
    for i in $(seq 1 36); do
        sleep 5
        is_up && break
    done
fi

if ! is_up; then
    echo "!!! Il rig non risponde su SSH dopo il WOL. Controllo manuale necessario." >&2
    exit 1
fi

CURRENT=$(current_role || echo "sconosciuto")
echo "Ruolo attualmente attivo: $CURRENT"

if [ "$CURRENT" = "$TARGET" ]; then
    echo "Gia' sul ruolo richiesto ($TARGET). Nulla da fare."
    exit 0
fi

echo "Passo da '$CURRENT' a '$TARGET': grub-reboot + reboot..."
ssh $SSH_OPTS "${RIG_USER}@${RIG_IP}" "sudo grub-reboot \"${ENTRY_NAME}\" && sudo systemctl reboot"

echo "Riavvio inviato. Attendo che ${TARGET} risponda (max 3 min)..."
for i in $(seq 1 36); do
    sleep 5
    if is_up && [ "$(current_role || true)" = "$TARGET" ]; then
        echo "✅ Rig ora su ruolo: $TARGET"
        exit 0
    fi
done
echo "!!! Timeout: non confermato il passaggio a $TARGET, verifica manualmente." >&2
exit 1
