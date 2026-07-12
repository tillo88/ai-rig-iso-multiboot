#!/usr/bin/env python3
"""
HERMES Telegram Bot — dedicato al ruolo HERMES (chat con memoria).
Vive sul disco/systemd del ruolo hermes: parte quando il rig boota in hermes.

Perche' un bot dedicato: cosi' hai una chat Telegram SOLO per Hermes, senza
"impastarla" col bot Pi (che invece serve a controllare/accendere il rig). Ogni
messaggio viene inoltrato a Hermes-Agent in modalita' one-shot (`hermes -z`), che
mantiene AutoMem (memoria persistente sul 4o disco), web search (SearXNG) e i
tool — cioe' la vera "chat con memoria", non una semplice chiamata al modello.

Config in /etc/hermes-bot/config.env (NON nel codice: vedi hermes-bot.env.example).
Stdlib-only, stesso pattern di wol/devin/teacher.

Comandi:
  /status        health del llama-server hermes + ruolo attivo
  /help          questo messaggio
  testo libero   inoltrato a Hermes-Agent (`hermes -z ...`) e ti riporto la risposta

NB: gira come utente 'tillo' (User=tillo nel .service) perche' Hermes-Agent e la
sua config (~/.hermes) sono installati per tillo.
"""

import os
import re
import sys
import time
import json
import logging
import subprocess
import urllib.request
from pathlib import Path

# =============================================================================
# Config esterna, mai hardcoded (stesso principio degli altri bot).
# =============================================================================
CONFIG_PATH = Path(os.environ.get("HERMES_BOT_CONFIG", "/etc/hermes-bot/config.env"))


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
    required = ["BOT_TOKEN", "ALLOWED_CHAT_IDS"]
    missing = [k for k in required if not cfg.get(k) or cfg[k].startswith("CHANGEME")]
    if missing:
        print(f"Errore: compila questi campi in {path}: {missing}", file=sys.stderr)
        sys.exit(1)
    return cfg


CFG = load_config(CONFIG_PATH)
BOT_TOKEN = CFG["BOT_TOKEN"]
ALLOWED_CHAT_IDS = {c.strip() for c in CFG["ALLOWED_CHAT_IDS"].split(",") if c.strip()}
HERMES_BIN = CFG.get("HERMES_BIN", "hermes")
HERMES_ONESHOT_FLAG = CFG.get("HERMES_ONESHOT_FLAG", "-z")
HERMES_TIMEOUT_SECONDS = int(CFG.get("HERMES_TIMEOUT_SECONDS", "300"))  # 40B denso: lento
HERMES_HOME = CFG.get("HERMES_HOME", str(Path.home()))
LLAMA_URL = CFG.get("LLAMA_URL", "http://localhost:8080").rstrip("/")
ROLE_FILE = CFG.get("ROLE_FILE", "/etc/ai-rig/role")
POLL_INTERVAL_SECONDS = int(CFG.get("POLL_INTERVAL_SECONDS", "15"))

_handlers = [logging.StreamHandler()]
try:
    _handlers.append(logging.FileHandler("/var/log/hermes-bot.log"))
except (PermissionError, OSError):
    pass
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=_handlers,
)
logger = logging.getLogger(__name__)

# =============================================================================
# Stato persistito: solo offset long-poll (la memoria della conversazione vive
# in AutoMem lato Hermes-Agent, non qui).
# =============================================================================
STATE_FILE = Path("/var/lib/hermes-bot/state.json")
try:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.touch(exist_ok=True)
except (PermissionError, OSError):
    STATE_FILE = Path.home() / ".hermes-bot-state.json"


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
# Telegram (stdlib-only). send_long: Hermes puo' rispondere lungo -> spezziamo
# nei 4096 char del limite Telegram invece di troncare.
# =============================================================================
def _send_one(chat_id, text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True}
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                logger.error(f"Errore invio messaggio: {resp.read()}")
    except Exception as e:
        logger.error(f"Errore invio risposta: {e}")


def send_message(chat_id, text: str) -> None:
    text = text or "(nessuna risposta)"
    # spezza su ~3900 char rispettando per quanto possibile gli a-capo
    limit = 3900
    while text:
        if len(text) <= limit:
            _send_one(chat_id, text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        _send_one(chat_id, text[:cut])
        text = text[cut:].lstrip("\n")


def is_authorized(chat_id) -> bool:
    return str(chat_id) in ALLOWED_CHAT_IDS


# =============================================================================
# Hermes-Agent CLI + stato llama-server
# =============================================================================
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def ask_hermes(message: str) -> str:
    """Inoltra a Hermes-Agent one-shot. La risposta arriva su stdout (confermato
    da hermes-extras.sh.tmpl, che fa `hermes -z ... | grep OK`). Env con HOME di
    tillo cosi' trova ~/.hermes. Fail-soft: messaggio d'errore leggibile."""
    env = dict(os.environ)
    env["HOME"] = HERMES_HOME
    try:
        r = subprocess.run(
            [HERMES_BIN, HERMES_ONESHOT_FLAG, message],
            capture_output=True, text=True, timeout=HERMES_TIMEOUT_SECONDS,
            env=env, cwd=HERMES_HOME)
    except subprocess.TimeoutExpired:
        return f"⏱️ Hermes non ha risposto entro {HERMES_TIMEOUT_SECONDS}s (modello 40B, può capitare su richieste pesanti)."
    except FileNotFoundError:
        return (f"❌ Comando '{HERMES_BIN}' non trovato. Hermes-Agent è installato? "
                "Verifica con: hermes --version")
    except Exception as e:
        return f"❌ Errore lanciando Hermes: {e}"
    out = strip_ansi(r.stdout).strip()
    if not out:
        err = strip_ansi(r.stderr).strip()
        if err:
            return f"⚠️ Hermes non ha prodotto output. stderr:\n{err[:1500]}"
        return "(nessuna risposta da Hermes)"
    return out


def health_check() -> bool:
    try:
        req = urllib.request.Request(f"{LLAMA_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_role() -> str:
    try:
        return Path(ROLE_FILE).read_text().strip()
    except Exception:
        return "sconosciuto"


HELP_TEXT = (
    "🧠 HERMES Bot — chat con memoria (AutoMem + web).\n\n"
    "Scrivimi qualsiasi cosa: la inoltro a Hermes-Agent e ti riporto la risposta.\n\n"
    "/status - stato del modello hermes + ruolo attivo\n"
    "/help - questo messaggio\n\n"
    "La memoria è persistente (AutoMem sul 4° disco): Hermes ricorda tra una "
    "sessione e l'altra, anche dopo un riavvio del rig."
)


def format_status() -> str:
    healthy = health_check()
    role = get_role()
    lines = [f"Hermes API: {'🟢 pronta' if healthy else '🔴 non risponde'} ({LLAMA_URL})",
             f"Ruolo attivo sul rig: {role}"]
    if role != "hermes":
        lines.append("⚠️ Il rig NON è in ruolo hermes: passa a hermes dal bot Pi (/hermes).")
    return "\n".join(lines)


def handle_message(text: str, chat_id: str) -> str:
    cmd = text.split("@")[0].split(maxsplit=1)[0] if text else ""
    if cmd in ("/help", "/start"):
        return HELP_TEXT
    if cmd == "/status":
        return format_status()
    if text.startswith("/"):
        # comando sconosciuto: se il rig non è pronto avvisa, altrimenti passa a Hermes
        if not health_check():
            return "Comando non riconosciuto e Hermes non è raggiungibile. /help per la lista."
    return ask_hermes(text)


# =============================================================================
# Main loop
# =============================================================================
def main():
    logger.info("HERMES Bot avviato.")
    offset = load_offset()

    while True:
        try:
            url = (f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
                   f"?offset={offset}&limit=5&timeout=30")
            with urllib.request.urlopen(urllib.request.Request(url), timeout=35) as response:
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
                chat_id = str(msg["chat"]["id"])
                text = (msg.get("text") or "").strip()
                username = msg["from"].get("username", "unknown")

                if not is_authorized(chat_id):
                    logger.warning(f"Rifiutato chat_id non autorizzato {chat_id} (@{username}).")
                    send_message(chat_id, "⛔ Non autorizzato.")
                    continue
                if not text:
                    continue

                logger.info(f"Messaggio da @{username} ({chat_id}): {text[:80]}")
                # feedback immediato: le risposte di un 40B denso possono tardare
                if not text.startswith("/"):
                    send_message(chat_id, "💭 Sto pensando...")
                try:
                    reply = handle_message(text, chat_id)
                except Exception as e:
                    logger.exception("Errore gestendo il messaggio")
                    reply = f"❌ Errore interno: {e}"
                if reply:
                    send_message(chat_id, reply)

        except Exception as e:
            logger.error(f"Errore nel loop Telegram: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
