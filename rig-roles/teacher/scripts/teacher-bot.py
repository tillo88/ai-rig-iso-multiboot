#!/usr/bin/env python3
"""
TEACHER Telegram Bot — dedicato al ruolo TEACHER (Qwen3-VL, vision/reasoning).
Vive sul disco/systemd del ruolo teacher: parte quando il rig boota in teacher,
non gira sugli altri ruoli. Serve a NON restare al buio quando ForgeStudio gira
da solo mentre sei fuori casa: sorveglia il llama-server, ti avvisa se cade, e
ti lascia riavviarlo o interrogarlo dal telefono.

Config in /etc/teacher-bot/config.env (NON nel codice: vedi teacher-bot.env.example).
Stesso pattern stdlib-only del bot WOL e del bot DEVIN (niente python-telegram-bot:
il Pi e il rig non hanno bisogno di dipendenze extra per un long-poll).

Comandi:
  /status           stato llama-server teacher (health + VRAM se disponibile) + ruolo attivo
  /restart          riavvia il servizio llama-server@teacher (watchdog manuale)
  /ask <testo>      manda una domanda al teacher (Qwen3-VL) e ti riporta la risposta
  /ask + FOTO       (foto con didascalia) query vision: il teacher guarda l'immagine
  /watch on|off     attiva/disattiva le notifiche automatiche del watchdog
  /help             questo messaggio

NB: gira come root (User=root nel .service) perche' /restart deve poter agire su
systemctl. Le uniche azioni sono verso chat_id autorizzati.
"""

import os
import sys
import time
import json
import base64
import logging
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

# =============================================================================
# Config — caricata da file esterno, MAI hardcoded (stesso principio wol/devin).
# =============================================================================
CONFIG_PATH = Path(os.environ.get("TEACHER_BOT_CONFIG", "/etc/teacher-bot/config.env"))


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
LLAMA_URL = CFG.get("LLAMA_URL", "http://localhost:8080").rstrip("/")
LLAMA_SERVICE = CFG.get("LLAMA_SERVICE", "llama-server@teacher.service")
ROLE_FILE = CFG.get("ROLE_FILE", "/etc/ai-rig/role")
MODEL_NAME = CFG.get("MODEL_NAME", "")  # llama-server ignora il campo, ma lo passiamo se dato
POLL_INTERVAL_SECONDS = int(CFG.get("POLL_INTERVAL_SECONDS", "15"))
WATCHDOG_FAIL_THRESHOLD = int(CFG.get("WATCHDOG_FAIL_THRESHOLD", "3"))
ASK_TIMEOUT_SECONDS = int(CFG.get("ASK_TIMEOUT_SECONDS", "300"))  # modello Thinking: lento
HEALTH_TIMEOUT_SECONDS = int(CFG.get("HEALTH_TIMEOUT_SECONDS", "8"))

_handlers = [logging.StreamHandler()]
try:
    _handlers.append(logging.FileHandler("/var/log/teacher-bot.log"))
except (PermissionError, OSError):
    pass
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=_handlers,
)
logger = logging.getLogger(__name__)

# =============================================================================
# Stato persistito: offset long-poll + stato watchdog (per non ri-notificare la
# stessa caduta a ogni poll) + flag notifiche on/off.
# =============================================================================
STATE_FILE = Path("/var/lib/teacher-bot/state.json")
try:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.touch(exist_ok=True)
except (PermissionError, OSError):
    STATE_FILE = Path.home() / ".teacher-bot-state.json"

_DEFAULT_STATE = {
    "offset": 0,
    "last_healthy": None,     # ultimo esito health noto (True/False/None)
    "consecutive_fail": 0,
    "notified_down": False,   # ho gia' avvisato per la caduta in corso?
    "watch_enabled": True,
    "last_periodic_check": 0,
}


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
        merged = dict(_DEFAULT_STATE)
        merged.update(data)
        return merged
    except Exception:
        return dict(_DEFAULT_STATE)


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state))
    except (PermissionError, OSError) as e:
        logger.warning(f"Impossibile salvare stato in {STATE_FILE}: {e}")


# =============================================================================
# Telegram (stdlib-only)
# =============================================================================
def send_message(chat_id, text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                logger.error(f"Errore invio messaggio: {resp.read()}")
    except Exception as e:
        logger.error(f"Errore invio risposta: {e}")


def send_to_all_allowed(text: str) -> None:
    for chat_id in ALLOWED_CHAT_IDS:
        send_message(chat_id, text)


def is_authorized(chat_id) -> bool:
    return str(chat_id) in ALLOWED_CHAT_IDS


def tg_get(path: str, timeout: int = 15):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{path}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"Telegram GET {path} fallita: {e}")
        return None


def download_telegram_file(file_id: str) -> str:
    """Scarica un file inviato su Telegram e lo ritorna in base64 (per /ask + foto)."""
    info = tg_get(f"getFile?file_id={file_id}", timeout=15)
    if not info or not info.get("ok"):
        raise RuntimeError("getFile fallita")
    file_path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as resp:
        return base64.b64encode(resp.read()).decode()


# =============================================================================
# Teacher llama-server
# =============================================================================
def health_check() -> bool:
    try:
        req = urllib.request.Request(f"{LLAMA_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT_SECONDS) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_role() -> str:
    try:
        return Path(ROLE_FILE).read_text().strip()
    except Exception:
        return "sconosciuto"


def restart_llama() -> str:
    try:
        r = subprocess.run(["systemctl", "restart", LLAMA_SERVICE],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:
        return f"❌ Impossibile lanciare systemctl: {e}"
    if r.returncode != 0:
        return (f"❌ Riavvio fallito (rc={r.returncode}): {r.stderr.strip()[:300]}\n"
                "Se il bot non gira come root, serve il permesso su systemctl.")
    # attendi che l'health torni su
    for _ in range(20):
        time.sleep(3)
        if health_check():
            return f"✅ {LLAMA_SERVICE} riavviato, API di nuovo pronta."
    return "⚠️ Servizio riavviato ma l'API non risponde ancora. Riprova /status tra poco."


def ask_teacher(prompt: str, image_b64: str = None) -> str:
    """Chat completion (testo o vision) verso il teacher. Ritorna testo o errore."""
    if image_b64:
        content = [
            {"type": "text", "text": prompt or "Descrivi cosa vedi nell'immagine."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]
    else:
        content = prompt
    payload = {
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 1024,
        "temperature": 0.7,
        "stream": False,
    }
    if MODEL_NAME:
        payload["model"] = MODEL_NAME
    try:
        req = urllib.request.Request(
            f"{LLAMA_URL}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=ASK_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip() or "(nessuna risposta)"
    except urllib.error.URLError as e:
        return f"❌ Teacher non raggiungibile: {e}. Prova /status o /restart."
    except Exception as e:
        return f"❌ Errore interrogando il teacher: {e}"


# =============================================================================
# Comandi
# =============================================================================
HELP_TEXT = (
    "🎓 TEACHER Bot — comandi:\n\n"
    "/status - stato llama-server teacher + ruolo attivo\n"
    "/restart - riavvia il servizio llama-server@teacher\n"
    "/ask <testo> - domanda al teacher (Qwen3-VL)\n"
    "/ask + foto (con didascalia) - query vision sull'immagine\n"
    "/watch on|off - notifiche automatiche del watchdog\n"
    "/help - questo messaggio\n\n"
    "Per un reboot completo del rig usa il bot Pi (/teacher, /wakeup)."
)


def format_status(state: dict) -> str:
    healthy = health_check()
    role = get_role()
    lines = [f"Teacher API: {'🟢 pronta' if healthy else '🔴 non risponde'} ({LLAMA_URL})"]
    lines.append(f"Ruolo attivo sul rig: {role}")
    if role != "teacher":
        lines.append("⚠️ Il rig NON è in ruolo teacher: ForgeStudio non troverà l'insegnante.")
    lines.append(f"Watchdog: {'attivo' if state.get('watch_enabled') else 'disattivato'}")
    return "\n".join(lines)


def handle_command(update_msg: dict, chat_id: str, state: dict) -> str:
    text = update_msg.get("text", "") or update_msg.get("caption", "") or ""
    text = text.strip()

    # Foto: query vision (con o senza /ask esplicito nella didascalia)
    if "photo" in update_msg:
        prompt = text
        for pref in ("/ask@", "/ask"):
            if prompt.startswith(pref):
                prompt = prompt.split(maxsplit=1)[1] if " " in prompt else ""
                break
        try:
            biggest = update_msg["photo"][-1]  # ultima = risoluzione massima
            img_b64 = download_telegram_file(biggest["file_id"])
        except Exception as e:
            return f"❌ Non sono riuscito a scaricare la foto: {e}"
        return ask_teacher(prompt, image_b64=img_b64)

    cmd_full = text.split(maxsplit=1)
    cmd = cmd_full[0].split("@")[0] if cmd_full else ""

    if cmd == "/status":
        return format_status(state)
    if cmd == "/restart":
        send_message(chat_id, f"🔄 Riavvio {LLAMA_SERVICE}...")
        return restart_llama()
    if cmd == "/ask":
        if len(cmd_full) < 2 or not cmd_full[1].strip():
            return "Uso: /ask <domanda>  (oppure invia una foto con didascalia)"
        return ask_teacher(cmd_full[1].strip())
    if cmd == "/watch":
        arg = cmd_full[1].strip().lower() if len(cmd_full) > 1 else ""
        if arg in ("on", "off"):
            state["watch_enabled"] = (arg == "on")
            save_state(state)
            return f"Watchdog {'attivato' if arg == 'on' else 'disattivato'}."
        return "Uso: /watch on  oppure  /watch off"
    if cmd in ("/help", "/start"):
        return HELP_TEXT
    # Testo libero senza comando -> lo trattiamo come /ask, comodo da telefono
    if text and not text.startswith("/"):
        return ask_teacher(text)
    return "Comando non riconosciuto. /help per la lista."


# =============================================================================
# Watchdog (folded nel loop, come i task periodici del bot DEVIN)
# =============================================================================
def run_watchdog(state: dict) -> None:
    if not state.get("watch_enabled", True):
        return
    healthy = health_check()

    if healthy:
        if state.get("notified_down"):
            send_to_all_allowed("🟢 Teacher di nuovo raggiungibile: il llama-server risponde.")
        state["consecutive_fail"] = 0
        state["notified_down"] = False
    else:
        state["consecutive_fail"] = state.get("consecutive_fail", 0) + 1
        if state["consecutive_fail"] >= WATCHDOG_FAIL_THRESHOLD and not state.get("notified_down"):
            send_to_all_allowed(
                f"🔴 Teacher NON risponde da {state['consecutive_fail']} controlli "
                f"(~{state['consecutive_fail'] * POLL_INTERVAL_SECONDS}s).\n"
                "ForgeStudio è senza insegnante (resta solo lo studente UI-TARS).\n"
                "Prova /restart. Se il rig è bloccato del tutto, riavvialo dal bot Pi.")
            state["notified_down"] = True

    state["last_healthy"] = healthy


# =============================================================================
# Main loop
# =============================================================================
def main():
    logger.info("TEACHER Bot avviato.")
    state = load_state()
    poll_timeout = min(20, POLL_INTERVAL_SECONDS)

    while True:
        try:
            url = (f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
                   f"?offset={state['offset']}&limit=5&timeout={poll_timeout}")
            with urllib.request.urlopen(urllib.request.Request(url), timeout=poll_timeout + 5) as response:
                data = json.loads(response.read().decode("utf-8"))

            if not data.get("ok"):
                logger.error(f"API error: {data}")
                time.sleep(5)
            else:
                for update in data.get("result", []):
                    state["offset"] = update["update_id"] + 1
                    save_state(state)

                    if "message" not in update:
                        continue
                    msg = update["message"]
                    chat_id = str(msg["chat"]["id"])
                    username = msg["from"].get("username", "unknown")

                    if not is_authorized(chat_id):
                        logger.warning(f"Rifiutato chat_id non autorizzato {chat_id} (@{username}).")
                        send_message(chat_id, "⛔ Non autorizzato.")
                        continue

                    logger.info(f"Update da @{username} ({chat_id})")
                    try:
                        reply = handle_command(msg, chat_id, state)
                    except Exception as e:
                        logger.exception("Errore gestendo il comando")
                        reply = f"❌ Errore interno: {e}"
                    if reply:
                        send_message(chat_id, reply)

        except Exception as e:
            logger.error(f"Errore nel loop Telegram: {e}")
            time.sleep(5)

        now_ts = time.time()
        if now_ts - state.get("last_periodic_check", 0) >= POLL_INTERVAL_SECONDS:
            try:
                run_watchdog(state)
            except Exception as e:
                logger.error(f"Errore watchdog: {e}")
            state["last_periodic_check"] = now_ts
            save_state(state)


if __name__ == "__main__":
    main()
