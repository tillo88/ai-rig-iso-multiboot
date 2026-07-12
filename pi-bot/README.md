# Bot Telegram sul Raspberry Pi — deploy passo-passo

Il bot ti fa accendere il rig e scegliere il ruolo dal telefono:
`/wakeup` `/devin` `/hermes` `/teacher` `/status` `/verify`

## Prerequisiti
- Raspberry con Raspberry Pi OS (Bullseye/Bookworm)
- Il token del bot da @BotFather (quello che avevi salvato in `config/rig.env`)
- Il tuo chat_id Telegram: scrivi a **@userinfobot** su Telegram, te lo dice lui

> ℹ️ **Utente e percorsi**: la guida usa l'utente `pi` e `/home/pi/ai-rig-wol-bot/`.
> Se il tuo utente è diverso (es. `tillo`) o preferisci un altro percorso (es.
> `/opt/ai-rig-bot/`), va benissimo: sostituisci coerentemente in TUTTI i
> comandi qui sotto **e** in `ai-rig-wol-bot.service` (righe `User=` ed
> `ExecStart=`). I `chown` vanno fatti verso l'utente che esegue il servizio —
> se non combaciano, il bot logga `Permission denied` su `/var/lib/ai-rig-bot`
> (dal quale ora comunque si difende con un fallback nella home).

## Installazione (copia-incolla, riga per riga)

```bash
# 1. Dipendenze
sudo apt install -y python3 wakeonlan

# 2. Cartelle + file di log (il bot gira come 'pi', che non puo' creare file in /var/log)
sudo mkdir -p /home/pi/ai-rig-wol-bot /etc/ai-rig-bot /var/lib/ai-rig-bot
sudo chown pi:pi /var/lib/ai-rig-bot
sudo touch /var/log/ai-rig-wol-bot.log
sudo chown pi:pi /var/log/ai-rig-wol-bot.log

# 3. Copia i file (da questa cartella pi-bot/ del progetto)
sudo cp ai-rig-wol-bot.py /home/pi/ai-rig-wol-bot/
sudo cp config.env.example /etc/ai-rig-bot/config.env

# 4. Compila la config — DEVI cambiare: BOT_TOKEN, RIG_MAC, ALLOWED_CHAT_IDS
sudo nano /etc/ai-rig-bot/config.env

# 5. Proteggi la config (contiene il token!)
sudo chmod 600 /etc/ai-rig-bot/config.env
sudo chown pi:pi /etc/ai-rig-bot/config.env /home/pi/ai-rig-wol-bot/ai-rig-wol-bot.py

# 6. Chiave SSH del bot verso il rig — SALTA se l'hai gia' generata
sudo -u pi ssh-keygen -t ed25519 -C "pi@ai-rig-bot" -f /home/pi/.ssh/ai_rig -N ""
# La .pub va COPIATA in cache/ai-rig-bot.pub del progetto ISO **prima** di
# costruire la ISO: e' cosi' che finisce autorizzata su tutti e 3 i dischi.

# 7. Servizio: parte da solo ad ogni riavvio del Pi
sudo cp ai-rig-wol-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-rig-wol-bot

# 8. Guarda i log in diretta (Ctrl+C per uscire)
journalctl -u ai-rig-wol-bot -f
```

## Test
1. Scrivi `/help` al bot su Telegram → deve rispondere con la lista comandi.
2. Se risponde "⛔ Non autorizzato": guarda in `journalctl -u ai-rig-wol-bot`,
   il tuo chat_id è nel log del comando rifiutato. Copialo in
   `ALLOWED_CHAT_IDS` in `/etc/ai-rig-bot/config.env`, poi:
   `sudo systemctl restart ai-rig-wol-bot`
3. `/status` funziona subito; `/devin` `/hermes` `/teacher` funzionano solo
   DOPO che sul rig hai eseguito `grub-stable-entries.sh` (vedi README
   principale, Parte 3.4).

## Note
- La config sta in `/etc/ai-rig-bot/config.env`, MAI nel codice: aggiornare lo
  script in futuro non ti fa perdere niente.
- Il bot ricorda a che punto era (offset in `/var/lib/ai-rig-bot/`): un riavvio
  non ri-esegue i comandi vecchi.
- `ENABLE_NETBOOT_SELECT` lascia `false`: è la Fase B avanzata
  (`scripts/README-netboot.md`), da attivare solo dopo averla verificata a mano.
