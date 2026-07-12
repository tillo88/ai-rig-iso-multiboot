#!/usr/bin/env python3
"""
AI Rig Telegram WOL Bot — v2
Per Raspberry Pi (Bullseye/Bookworm). Config in /etc/ai-rig-bot/config.env (NON nel
codice: vedi config.env.example).
Comandi: /wakeup, /status, /verify, /devin, /hermes, /teacher, /help
"""

import os
import sys
import time
import json
import logging
import subprocess
from pathlib import Path

# =============================================================================
# Config — caricata da file esterno, MAI hardcoded qui (era il bug originale:
# i controlli "if TOKEN == placeholder" confrontavano stringhe che non
# corrispondevano piu' al valore reale, quindi passavano anche con valori
# rotti e il bot falliva in modo silenzioso/confuso).
# =============================================================================
CONFIG_PATH = Path(os.environ.get("AI_RIG_BOT_CONFIG", "/etc/ai-rig-bot/config.env"))


def load_config(path: Path) -> dict:
    if not path.is_file():
        print(f"Errore: file di config non trovato: {path}", file=sys.stderr)
        sys.exit(1)
    cfg = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
    required = ["BOT_TOKEN", "RIG_MAC", "RIG_IP", "ALLOWED_CHAT_IDS"]
    missing = [k for k in required if not cfg.get(k) or cfg[k].startswith("CHANGEME")]
    if missing:
        print(f"Errore: compila questi campi in {path}: {missing}", file=sys.stderr)
        sys.exit(1)
    return cfg


CFG = load_config(CONFIG_PATH)
BOT_TOKEN = CFG["BOT_TOKEN"]
RIG_MAC = CFG["RIG_MAC"]
RIG_IP = CFG["RIG_IP"]
RIG_API_PORT = int(CFG.get("RIG_API_PORT", "8080"))
RIG_SSH_USER = CFG.get("RIG_SSH_USER", "tillo")
RIG_SSH_KEY = CFG.get("RIG_SSH_KEY", "")
RIG_VERIFY_LOG = CFG.get("RIG_VERIFY_LOG", "/var/log/ai-rig-verify.log")
ALLOWED_CHAT_IDS = {c.strip() for c in CFG["ALLOWED_CHAT_IDS"].split(",") if c.strip()}
ENABLE_NETBOOT_SELECT = CFG.get("ENABLE_NETBOOT_SELECT", "false").lower() == "true"
TFTP_GRUB_TARGET_FILE = CFG.get("TFTP_GRUB_TARGET_FILE", "/srv/tftp/grub_target")

ROLES = ("devin", "hermes", "teacher")

# Log: journald cattura gia' stdout (StreamHandler) via systemd. Il file in
# /var/log e' un extra comodo, ma l'utente 'pi' non puo' crearlo da solo: se
# manca o non e' scrivibile, prosegui senza invece di crashare in loop.
_handlers = [logging.StreamHandler()]
try:
    _handlers.append(logging.FileHandler("/var/log/ai-rig-wol-bot.log"))
except (PermissionError, OSError):
    pass
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=_handlers,
)
logger = logging.getLogger(__name__)


# =============================================================================
# Stato (offset long-poll) — persistito, altrimenti un riavvio del bot rilegge
# tutto il backlog di Telegram e puo' ri-eseguire comandi vecchi (es. un
# /wakeup o un cambio ruolo che pensavi gia' concluso).
# Se /var/lib/ai-rig-bot non e' scrivibile dall'utente del servizio (visto sul
# campo: utente 'tillo' e non 'pi'), fallback nella home invece di riempire i
# log di Permission denied ad ogni update.
# =============================================================================
STATE_FILE = Path("/var/lib/ai-rig-bot/state.json")
try:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.touch(exist_ok=True)
except (PermissionError, OSError):
    STATE_FILE = Path.home() / ".ai-rig-bot-state.json"


def load_offset() -> int:
    try:
        return json.loads(STATE_FILE.read_text()).get("offset", 0)
    except Exception:
        return 0


def save_offset(offset: int) -> None:
    try:
        STATE_FILE.write_text(json.dumps({"offset": offset}))
    except (PermissionError, OSError) as e:
        logger.warning(f"Impossibile salvare offset in {STATE_FILE}: {e}")


# =============================================================================
# SSH helper — chiave esplicita + BatchMode (fallisce subito invece di restare
# appeso su un prompt password se la chiave non e' autorizzata).
# =============================================================================
def ssh_run(cmd: str, timeout: int = 10) -> subprocess.CompletedProcess:
    ssh_cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=accept-new"]
    if RIG_SSH_KEY:
        ssh_cmd += ["-i", RIG_SSH_KEY]
    ssh_cmd += [f"{RIG_SSH_USER}@{RIG_IP}", cmd]
    return subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)


def send_wol(mac_address: str) -> bool:
    try:
        r = subprocess.run(["wakeonlan", mac_address], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            logger.info(f"WOL inviato a {mac_address}")
            return True
    except FileNotFoundError:
        pass
    try:
        r = subprocess.run(["ether-wake", mac_address], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            logger.info(f"WOL inviato via ether-wake a {mac_address}")
            return True
    except FileNotFoundError:
        pass
    try:
        import socket
        mac_bytes = bytes.fromhex(mac_address.replace(":", "").replace("-", ""))
        magic_packet = b"\xff" * 6 + mac_bytes * 16
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(magic_packet, ("<broadcast>", 9))
        sock.close()
        logger.info(f"WOL inviato via Python puro a {mac_address}")
        return True
    except Exception as e:
        logger.error(f"WOL fallito: {e}")
        return False


def is_online() -> bool:
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "2", RIG_IP],
                            capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def check_rig_status() -> dict:
    status = {"online": False, "api_ready": False, "ping_ms": None, "role": None}
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "2", RIG_IP],
                            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            status["online"] = True
            for line in r.stdout.split("\n"):
                if "time=" in line:
                    try:
                        status["ping_ms"] = line.split("time=")[1].split(" ")[0]
                    except (IndexError, ValueError):
                        pass
    except Exception as e:
        logger.debug(f"Ping fallito: {e}")

    if status["online"]:
        try:
            import urllib.request
            req = urllib.request.Request(f"http://{RIG_IP}:{RIG_API_PORT}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as response:
                status["api_ready"] = response.status == 200
        except Exception as e:
            logger.debug(f"API check fallito: {e}")
        try:
            r = ssh_run("cat /etc/ai-rig/role", timeout=8)
            if r.returncode == 0 and r.stdout.strip():
                status["role"] = r.stdout.strip()
        except Exception as e:
            logger.debug(f"Lettura ruolo fallita: {e}")

    return status


def get_verify_log() -> str:
    try:
        r = ssh_run(f"cat {RIG_VERIFY_LOG} 2>/dev/null || echo 'Log non trovato'", timeout=10)
        return r.stdout if r.returncode == 0 else f"Errore SSH: {r.stderr}"
    except Exception as e:
        return f"Impossibile connettersi: {e}"


def format_status(status: dict) -> str:
    if not status["online"]:
        return "🔴 Rig OFFLINE\n\nNon risponde al ping."
    msg = "🟢 Rig ONLINE"
    if status["ping_ms"]:
        msg += f" (ping: {status['ping_ms']}ms)"
    msg += "\n"
    msg += "✅ API llama-server pronta\n" if status["api_ready"] else "⏳ API non ancora pronta\n"
    msg += f"Ruolo attivo: {status['role'] or 'sconosciuto (SSH?)'}\n"
    return msg


# =============================================================================
# Cambio ruolo — Fase A (grub-reboot via SSH, robusta) sempre disponibile.
# Fase B (GRUB legge da TFTP a rig spento) solo se ENABLE_NETBOOT_SELECT=true
# E gia' verificata manualmente (vedi README-netboot.md). Di default OFF.
# =============================================================================
def grub_id_for(role: str) -> str:
    # Deve corrispondere al --id assegnato nelle custom menuentry (vedi
    # scripts/grub-stable-entries.sh nel progetto ISO). NON usare il titolo
    # testuale: i titoli generati da os-prober cambiano con la versione kernel.
    return role


def netboot_select(role: str) -> None:
    Path(TFTP_GRUB_TARGET_FILE).write_text(f'set default="{grub_id_for(role)}"\n')
    send_wol(RIG_MAC)
    time.sleep(15)  # tempo per far leggere il file a GRUB prima di pulirlo (vedi caveat in README-netboot.md)
    Path(TFTP_GRUB_TARGET_FILE).write_text("")


def switch_role(target: str) -> str:
    if target not in ROLES:
        return f"Ruolo sconosciuto: {target}"

    if not is_online():
        if ENABLE_NETBOOT_SELECT:
            logger.info(f"Rig spento, netboot-select verso {target}")
            netboot_select(target)
        else:
            logger.info(f"Rig spento, WOL semplice (bootera' sul ruolo di default)")
            send_wol(RIG_MAC)
        logger.info("Attendo boot (max 3 min)...")
        for _ in range(36):
            time.sleep(5)
            if is_online():
                break
        if not is_online():
            return "❌ Il rig non risponde su ping dopo il WOL. Controllo manuale necessario."

    # a questo punto il rig e' online: verifico il ruolo attivo
    for _ in range(6):
        r = ssh_run("cat /etc/ai-rig/role", timeout=8)
        if r.returncode == 0:
            break
        time.sleep(5)
    else:
        return "❌ Rig online ma SSH non risponde ancora. Riprova tra poco."

    current = r.stdout.strip()
    if current == target:
        return f"✅ Gia' sul ruolo richiesto ({target})."

    entry_id = grub_id_for(target)
    r = ssh_run(f"sudo grub-reboot '{entry_id}' && sudo systemctl reboot", timeout=10)
    if r.returncode != 0:
        return f"❌ grub-reboot fallito: {r.stderr.strip()[:300]}"

    logger.info(f"Riavvio verso {target} inviato, attendo conferma (max 3 min)...")
    time.sleep(20)  # margine minimo perche' il reboot parta davvero
    for _ in range(30):
        time.sleep(5)
        if is_online():
            rr = ssh_run("cat /etc/ai-rig/role", timeout=8)
            if rr.returncode == 0 and rr.stdout.strip() == target:
                return f"✅ Rig ora su ruolo: {target}"
    return f"⚠️ Riavvio inviato ma non confermato entro 3 min. Verifica con /status."


# =============================================================================
# Telegram helpers
# =============================================================================
MD_ESCAPE_CHARS = r"_*[]()~`>#+-=|{}.!"


def escape_markdown(text: str) -> str:
    # Serve solo per contenuto DINAMICO (log, output comandi): senza questo, Telegram
    # rifiuta (HTTP 400) qualunque messaggio con _ * ` [ non bilanciati — il bug
    # originale usava parse_mode=Markdown anche sul log di verify, che quasi
    # certamente contiene questi caratteri, quindi /verify falliva silenziosamente.
    return "".join("\\" + c if c in MD_ESCAPE_CHARS else c for c in text)


def send_message(chat_id, text: str, markdown: bool = True) -> None:
    import urllib.request
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
    if markdown:
        payload["parse_mode"] = "Markdown"
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.error(f"Errore invio messaggio: {resp.read()}")
    except Exception as e:
        logger.error(f"Errore invio risposta: {e}")


def is_authorized(chat_id) -> bool:
    return str(chat_id) in ALLOWED_CHAT_IDS


HELP_TEXT = (
    "🤖 *AI Rig WOL Bot*\n\n"
    "• /wakeup - Accende il rig (ruolo di default)\n"
    "• /status - Stato + ruolo attivo\n"
    "• /verify - Ultimo report di verifica\n"
    "• /devin /hermes /teacher - Accende (se serve) e passa a quel ruolo\n"
    "• /help - Questo messaggio\n\n"
    f"Rig: `{RIG_IP}`"
)


def handle_command(text: str, chat_id) -> str:
    cmd = text.split("@")[0].strip()

    if cmd == "/wakeup":
        return ("⚡ Pacchetto WOL inviato. Il rig dovrebbe avviarsi in 10-30s (ruolo di default)."
                if send_wol(RIG_MAC) else "❌ Errore invio WOL.")
    if cmd == "/status":
        return format_status(check_rig_status())
    if cmd == "/verify":
        return "📋 Ultimo report:\n\n```\n" + escape_markdown(get_verify_log())[:3500] + "\n```"
    if cmd.lstrip("/") in ROLES:
        return switch_role(cmd.lstrip("/"))
    if cmd in ("/help", "/start"):
        return HELP_TEXT
    return "Comando non riconosciuto. /help per la lista."


def main():
    logger.info("Bot avviato.")
    import urllib.request
    offset = load_offset()

    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}&limit=5&timeout=30"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=35) as response:
                data = json.loads(response.read().decode("utf-8"))

            if not data.get("ok"):
                logger.error(f"API error: {data}")
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                save_offset(offset)

                if "message" not in update:
                    continue
                msg = update["message"]
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "").strip()
                username = msg["from"].get("username", "unknown")

                if not is_authorized(chat_id):
                    logger.warning(f"Comando rifiutato da chat_id non autorizzato {chat_id} (@{username}): {text}")
                    send_message(chat_id, "⛔ Non autorizzato.", markdown=False)
                    continue

                logger.info(f"Comando da @{username} ({chat_id}): {text}")
                try:
                    reply = handle_command(text, chat_id)
                except Exception as e:
                    logger.exception("Errore gestendo il comando")
                    reply = f"❌ Errore interno: {e}"
                if reply:
                    send_message(chat_id, reply)

        except Exception as e:
            logger.error(f"Errore nel loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
