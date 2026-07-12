import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Carica .env dalla cartella di QUESTO file (devin/ui/.env), non dalla CWD del
# processo — stesso principio del fix su CONFIG_PATH qui sotto. Se il file non
# esiste, load_dotenv() non fa nulla (nessun errore): restano valide le vere
# variabili d'ambiente di sistema, se qualcuno le usa invece del .env.
from dotenv import load_dotenv
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)

print(f"[STARTUP] .env: {'trovato in ' + str(_ENV_PATH) if _ENV_PATH.exists() else 'NON trovato in ' + str(_ENV_PATH)}")
print(f"[STARTUP] TINYFISH_API_KEY: {'presente (' + os.environ['TINYFISH_API_KEY'][:8] + '...)' if os.getenv('TINYFISH_API_KEY') else 'ASSENTE — la web search TinyFish fallira con questo messaggio esatto'}")

# FIX: path assoluto, non piu' relativo alla CWD del processo (era la causa diretta
# di "[FATAL] [Errno 2] No such file or directory: 'config/settings.json'" quando
# il server veniva avviato da una directory diversa dalla root del progetto).
CONFIG_PATH = str(ROOT / "config" / "settings.json")

import json
import asyncio
import time
import threading
import base64
import webbrowser
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from devin.core.orchestrator import Orchestrator, LOG_DIR
from devin.core.chat_persistence import ChatPersistence
from devin.ai.client import AIClient
from devin.ai.local_model_launcher import LocalModelLauncher
from devin.ai.autocomplete import Autocomplete
from devin.ai.web_search import get_web_search_provider, format_results_as_context

app = FastAPI(title="DEVIN AI IDE")

# FIX: Disabilita cache Jinja2
from jinja2 import FileSystemLoader, Environment
jinja_env = Environment(
    loader=FileSystemLoader(str(ROOT / "devin/ui/templates")),
    auto_reload=True,
    cache_size=0
)
templates = Jinja2Templates(env=jinja_env)

# === RUNTIME STATE ===
active_runs = {}
runs_lock = threading.Lock()
_model_launcher = None
_ai_client = None
_autocomplete = None


def _get_launcher():
    global _model_launcher
    if _model_launcher is None:
        try:
            _model_launcher = LocalModelLauncher.from_config(CONFIG_PATH)
        except Exception as e:
            print(f"[WARN] Could not init launcher: {e}")
    return _model_launcher


def _get_ai_client():
    global _ai_client
    if _ai_client is None:
        _ai_client = AIClient()
    return _ai_client


def _get_autocomplete():
    # FIX (bug 1.2 report): riusa il client/istanza singleton invece di ricrearla
    # ad ogni keystroke-trigger (ogni AIClient() nuovo fa 2x health-check + WOL).
    global _autocomplete
    if _autocomplete is None:
        _autocomplete = Autocomplete(ai_client=_get_ai_client())
    return _autocomplete


def _get_vram_info():
    """Ritorna VRAM info da nvidia-smi se disponibile."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        if lines:
            parts = [p.strip() for p in lines[0].split(",")]
            return {
                "gpu_name": parts[0],
                "total_mb": int(float(parts[1])),
                "used_mb": int(float(parts[2])),
                "free_mb": int(float(parts[3]))
            }
    except Exception:
        pass
    return None


def _detect_mode(message: str) -> str:
    """Rileva se la domanda richiede reasoning o coding."""
    msg_lower = message.lower()
    coding_keywords = [
        "code", "codice", "python", "function", "def ", "class ",
        "bug", "fix", "patch", "diff", "write a", "scrivi", "implementa",
        "crea una funzione", "crea una classe", "refactor", "debug",
        "syntax", "import ", "error", "exception", "traceback",
        "javascript", "html", "css", "sql", "api", "json", "xml",
        "loop", "array", "dict", "list", "tuple", "async", "await"
    ]
    reasoning_keywords = [
        "explain", "spiega", "why", "perche", "how does", "come funziona",
        "architecture", "design", "pattern", "best practice", "approccio",
        "strategia", "piano", "analizza", "compare", "confronta",
        "philosophy", "concept", "theory", "principle"
    ]

    coding_score = sum(1 for k in coding_keywords if k in msg_lower)
    reasoning_score = sum(1 for k in reasoning_keywords if k in msg_lower)

    if coding_score > reasoning_score:
        return "coder"
    elif reasoning_score > coding_score:
        return "reasoning"

    return "coder"


def _is_scaffold_request(message: str, project_path: str) -> bool:
    """
    Euristica per il routing chat -> scaffolding (Regola Chat First):
    se il project_path non esiste o e' vuoto (nessun file), e il messaggio ha un
    verbo di creazione, instradiamo verso Zero-Shot Scaffolding invece della chat normale.
    """
    if not project_path:
        return False

    path = Path(project_path).expanduser()
    is_empty_or_missing = (not path.exists()) or (path.is_dir() and not any(path.rglob("*.py")))

    scaffold_verbs = [
        "crea un progetto", "crea una app", "crea un'app", "scaffold", "build a project",
        "create a project", "genera un progetto", "starter", "boilerplate", "da zero"
    ]
    msg_lower = message.lower()
    has_scaffold_intent = any(v in msg_lower for v in scaffold_verbs)

    return is_empty_or_missing and has_scaffold_intent


def _get_model_detail(alias: str, info: dict) -> dict:
    """Costruisce dettaglio completo di un modello."""
    client = _get_ai_client()
    config = client.config.get("models", {})
    local_models = config.get("local_models", {})

    config_key = "reasoning" if alias in ("planner", "reasoning") else "coder"
    model_cfg = local_models.get(config_key, {})

    detail = {
        "alias": alias,
        "port": info.get("port", "N/A"),
        "status": info.get("status", "unknown"),
        "online": info.get("status") == "running",
        "file": model_cfg.get("file", "unknown"),
        "fallback_file": model_cfg.get("fallback_file"),
        "description": model_cfg.get("description", ""),
        "ctx_size": model_cfg.get("ctx_size", "N/A"),
        "is_fallback_active": False
    }

    primary_file = model_cfg.get("file", "")
    models_dir = config.get("local_models_dir", "")
    if models_dir and primary_file:
        primary_path = Path(models_dir) / primary_file
        if not primary_path.exists() and detail["fallback_file"]:
            detail["is_fallback_active"] = True
            detail["active_file"] = detail["fallback_file"]
        else:
            detail["active_file"] = primary_file

    if config_key == "reasoning":
        vision = model_cfg.get("vision", {})
        detail["vision_enabled"] = vision.get("enabled", False)
        detail["vision_mmproj"] = vision.get("mmproj")
    else:
        detail["vision_enabled"] = False

    return detail


def _scan_project_files(project_path: str) -> list:
    """Scansiona i file di un progetto per il file explorer."""
    path = Path(project_path).expanduser()
    if not path.exists() or not path.is_dir():
        return []

    files = []
    try:
        for item in sorted(path.rglob("*")):
            if item.is_file() and not any(p.startswith(".") or p in ("__pycache__", "venv", ".venv", "node_modules") for p in item.parts):
                rel = item.relative_to(path)
                files.append({
                    "name": item.name,
                    "path": str(rel),
                    "full_path": str(item),
                    "size": item.stat().st_size,
                    "mtime": datetime.fromtimestamp(item.stat().st_mtime).isoformat(),
                    "is_python": item.suffix == ".py",
                    "is_text": item.suffix in (".py", ".json", ".yaml", ".yml", ".txt", ".md", ".sh", ".bat")
                })
    except Exception as e:
        print(f"[Explorer] Error scanning {path}: {e}")

    return files


def _read_file_content(file_path: str, max_chars: int = 10000) -> str:
    """Legge il contenuto di un file di testo."""
    path = Path(file_path).expanduser()
    if not path.exists() or not path.is_file():
        return ""

    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        if len(content) > max_chars:
            content = content[:max_chars] + "\n\n# [...file truncated...]"
        return content
    except Exception:
        return "# [Error reading file]"


# ============================================================
# PAGES
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Dashboard IDE principale."""
    client = _get_ai_client()
    health = client.health()
    launcher = _get_launcher()
    models_running = False
    models_info = []

    if launcher:
        status = launcher.get_status()
        models_running = bool(status.local_running)
        for alias, info in status.local_running.items():
            models_info.append(_get_model_detail(alias, info))

    # Lista progetti disponibili in workspace/
    workspace_path = ROOT / "workspace"
    projects = []
    if workspace_path.exists():
        for item in sorted(workspace_path.iterdir()):
            if item.is_dir():
                projects.append({
                    "name": item.name,
                    "path": str(item),
                    "has_python": any(item.rglob("*.py"))
                })

    # Run recenti
    recent_runs = []
    if LOG_DIR.exists():
        for f in sorted(LOG_DIR.glob("run_*.log"), reverse=True)[:10]:
            stat = f.stat()
            content = f.read_text(encoding="utf-8", errors="ignore")
            run_status = "unknown"
            if "status: success" in content.lower():
                run_status = "success"
            elif "status: failed" in content.lower():
                run_status = "failed"
            elif "status: timeout" in content.lower():
                run_status = "timeout"
            elif "status: stopped" in content.lower():
                run_status = "stopped"

            recent_runs.append({
                "run_id": f.stem,
                "status": run_status,
                "size": f.stat().st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "preview": content[:200]
            })

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "health": health,
            "models_running": models_running,
            "models_info": models_info,
            "vram": _get_vram_info(),
            "projects": projects,
            "recent_runs": recent_runs,
            "active_runs": list(active_runs.keys())
        }
    )


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    client = _get_ai_client()
    launcher = _get_launcher()

    models_chat_info = {}
    if launcher:
        status = launcher.get_status()
        for alias, info in status.local_running.items():
            models_chat_info[alias] = _get_model_detail(alias, info)

    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context={
            "models_info": models_chat_info,
            "vram": _get_vram_info()
        }
    )


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    runs = []
    if LOG_DIR.exists():
        for f in sorted(LOG_DIR.glob("run_*.log"), reverse=True):
            stat = f.stat()
            content = f.read_text(encoding="utf-8", errors="ignore")
            status = "unknown"
            if "status: success" in content.lower():
                status = "success"
            elif "status: failed" in content.lower():
                status = "failed"
            elif "status: timeout" in content.lower():
                status = "timeout"
            elif "status: stopped" in content.lower():
                status = "stopped"
            runs.append({
                "run_id": f.stem,
                "file": str(f.name),
                "size": f.stat().st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "status": status,
                "preview": content[:500]
            })
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={"runs": runs[:50]}
    )


# ============================================================
# API - FILE EXPLORER
# ============================================================

@app.get("/api/explore")
async def api_explore(path: str = ""):
    """Esplora file di un progetto."""
    if not path:
        return {"error": "missing path"}

    files = _scan_project_files(path)
    return {
        "path": path,
        "files": files,
        "count": len(files)
    }


@app.get("/api/file")
async def api_file(path: str = ""):
    """Legge contenuto di un file."""
    if not path:
        return {"error": "missing path"}

    content = _read_file_content(path)
    return {
        "path": path,
        "content": content,
        "language": Path(path).suffix.lstrip(".") or "text"
    }


# ============================================================
# API - MODELS INFO
# ============================================================

@app.get("/api/health")
async def api_health():
    """Health check rig/locale per il polling badge in dashboard (index.html)."""
    return _get_ai_client().health()


@app.get("/api/models/info")
async def api_models_info():
    launcher = _get_launcher()
    if not launcher:
        return {"running": False, "models": [], "vram": _get_vram_info()}

    status = launcher.get_status()
    models = []
    for alias, info in status.local_running.items():
        models.append(_get_model_detail(alias, info))

    return {
        "running": bool(status.local_running),
        "models": models,
        "vram": _get_vram_info(),
        "source": status.model_source
    }


# ============================================================
# API - CHAT (SSE VELOCE) + VISION + WEB SEARCH + SCAFFOLD ROUTING
# ============================================================

class ChatRequest(BaseModel):
    message: str
    mode: str = "auto"
    image_base64: Optional[str] = None
    project_path: Optional[str] = None
    use_web_search: bool = False
    history: Optional[list] = None  # [{"role": "user"/"assistant", "content": "..."}], gestito dal frontend


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    message = req.message.strip()
    if not message:
        return {"error": "empty message"}

    # Regola "Chat First": se sembra una richiesta di scaffolding su workspace vuoto,
    # instrada verso lo Zero-Shot Scaffolding invece della chat normale.
    if req.project_path and _is_scaffold_request(message, req.project_path):
        return await api_chat_scaffold(RunRequest(path=req.project_path, task=message))

    selected_mode = _detect_mode(message) if req.mode == "auto" else req.mode
    if req.image_base64:
        # Solo il modello reasoning ha --mmproj caricato (vedi settings.json ->
        # local_models.reasoning.vision / rig_models.vision_enabled). Il coder
        # non e' multimodale: instradarci un'immagine causa 500 da llama-server.
        selected_mode = "reasoning"

    launcher = _get_launcher()
    if launcher:
        launcher.ensure_models()

    ai = _get_ai_client()

    content = message
    web_search_error = None
    if req.use_web_search:
        try:
            provider = get_web_search_provider(ai.config)
            results = provider.search(message, max_results=5)
            web_context = format_results_as_context(results)
            content = f"Risultati ricerca web:\n{web_context}\n\nDomanda utente: {message}"
        except Exception as e:
            # FIX: prima l'errore finiva "[Web search non disponibile: {e}]" DENTRO
            # il content mandato al modello — che lo ignorava e rispondeva a caso,
            # lasciando l'utente senza alcun segnale visibile del perche'. Ora e'
            # tracciato a parte e mandato come evento SSE distinto (vedi sotto),
            # il messaggio al modello resta quello originale (nessun rumore extra).
            web_search_error = str(e)

    if req.image_base64:
        content = [
            {"type": "text", "text": content if isinstance(content, str) else message},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{req.image_base64}"}}
        ]

    # Persistenza server-side dello storico, per-progetto (prerequisito per
    # "continua in chat" da un run fallito, check periodico, e "genera patch
    # da questa conversazione"). Se c'e' un progetto, il server e' la fonte di
    # verita' — req.history del client viene ignorato (era solo un cache di
    # visualizzazione lato browser); senza progetto (chat generica) resta
    # invariato il comportamento client-side esistente.
    chat_persistence = ChatPersistence(req.project_path) if req.project_path else None
    if chat_persistence:
        persisted_history = chat_persistence.load()
        chat_persistence.append("user", message)  # subito, sopravvive anche a crash mid-stream
    else:
        persisted_history = req.history or []

    # Storico conversazione + system prompt configurabile (Regola: elastico, non hardcoded).
    # system_prompt vuoto di default -> comportamento chat generica invariato.
    chat_cfg = ai.config.get("chat", {})
    system_prompt = (chat_cfg.get("system_prompt") or "").strip()
    max_history = chat_cfg.get("max_history_messages", 20)

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if persisted_history:
        # Tronca ai piu' recenti max_history messaggi: protegge da OOM/contesto
        # locale limitato su run prolungate (vincolo hardware locale).
        messages.extend(persisted_history[-max_history:])
    messages.append({"role": "user", "content": content})

    model_name = (
        ai.local_reasoning_model
        if selected_mode == "reasoning"
        else ai.local_coder_model
    )

    config_key = "reasoning" if selected_mode == "reasoning" else "coder"
    model_cfg = ai.config.get("models", {}).get("local_models", {}).get(config_key, {})
    model_detail = {
        "name": model_name,
        "file": model_cfg.get("file", ""),
        "description": model_cfg.get("description", ""),
        "ctx_size": model_cfg.get("ctx_size", ""),
        "vision": model_cfg.get("vision", {}).get("enabled", False),
        "web_search_used": req.use_web_search,
    }

    async def generate_sse(model_name: str, model_detail: dict):
        token_count = 0
        start_time = time.time()
        full_response = ""

        if web_search_error:
            yield f"event: warning\ndata: {json.dumps({'message': f'Web search non disponibile: {web_search_error}'})}\n\n"

        yield f"event: meta\ndata: {json.dumps({'mode': selected_mode, 'model': model_name, 'detail': model_detail})}\n\n"

        try:
            for chunk in ai.stream(messages, mode=selected_mode):
                token_count += 1
                full_response += chunk
                yield f"data: {json.dumps({'token': chunk})}\n\n"
                await asyncio.sleep(0)

            elapsed = time.time() - start_time
            tps = round(token_count / elapsed, 1) if elapsed > 0 else 0
            yield f"event: done\ndata: {json.dumps({'tokens': token_count, 'tps': tps, 'elapsed': round(elapsed, 1)})}\n\n"

            if chat_persistence and full_response.strip():
                chat_persistence.append("assistant", full_response)

        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        generate_sse(model_name, model_detail),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@app.post("/api/chat/vision")
async def api_chat_vision(message: str = Form(""), image: UploadFile = File(None), mode: str = Form("auto")):
    image_b64 = None
    if image:
        contents = await image.read()
        image_b64 = base64.b64encode(contents).decode("utf-8")

    req = ChatRequest(message=message, mode=mode, image_base64=image_b64)
    return await api_chat(req)


@app.post("/api/chat/search")
async def api_chat_search(req: ChatRequest):
    """Endpoint esplicito: forza sempre la ricerca web indipendentemente da euristiche."""
    req.use_web_search = True
    return await api_chat(req)


@app.get("/api/chat/history")
async def api_chat_history_get(project_path: str = ""):
    """Storico persistito per un progetto — il frontend lo chiama al caricamento
    pagina o al cambio di project_path, cosi' la conversazione sopravvive a
    refresh/chiusura del browser. updated_at serve al bot Telegram per il check
    'nessuna risposta da N ore' senza dover tracciare timestamp per-messaggio."""
    if not project_path:
        return {"history": [], "updated_at": None}
    cp = ChatPersistence(project_path)
    return {"history": cp.load(), "updated_at": cp.last_updated()}


@app.post("/api/chat/history/clear")
async def api_chat_history_clear(request: Request):
    """Reset della conversazione persistita per un progetto (bottone 'Nuova conversazione')."""
    data = await request.json()
    project_path = data.get("project_path", "")
    if not project_path:
        return {"error": "missing project_path"}
    ChatPersistence(project_path).clear()
    return {"status": "cleared"}


@app.post("/api/chat/generate_patch")
async def api_chat_generate_patch(request: Request):
    """
    'Genera patch da questa conversazione e riprova': prende la conversazione
    chat persistita per il progetto e la usa come piano (salta il Planner),
    poi Coder->Patcher->Runner->Critic come nel Mantenimento normale. Stesso
    streaming SSE via /stream/{run_id} gia' usato da /api/run e /api/chat/scaffold.
    """
    data = await request.json()
    project_path = data.get("project_path", "")
    if not project_path:
        return {"error": "missing project_path"}

    history = ChatPersistence(project_path).load()
    if not history:
        return {"error": "nessuna conversazione salvata per questo progetto"}

    conversation_text = "\n\n".join(f"[{m['role'].upper()}]: {m['content']}" for m in history)

    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{run_id}.log"
    log_path.write_text(f"Patch da conversazione: {run_id}\n", encoding="utf-8")

    def sse_callback(msg, level):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{level.upper()}] {msg}\n")

    def _bg():
        try:
            with Orchestrator(
                config_path=CONFIG_PATH,
                project_path=project_path,
                sse_callback=sse_callback
            ) as orch:
                with runs_lock:
                    active_runs[run_id] = orch
                try:
                    result = orch.run_from_conversation(
                        conversation_text=conversation_text,
                        project_path=project_path,
                        run_id=run_id
                    )
                    # Niente scrittura qui: run_from_conversation() scrive gia' il
                    # footer 'status: X' internamente in ogni return path.
                finally:
                    with runs_lock:
                        active_runs.pop(run_id, None)
        except Exception as e:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[FATAL] {e}\nstatus: failed\n")

    threading.Thread(target=_bg, daemon=True).start()
    return {"run_id": run_id, "status": "started", "mode": "chat_patch"}


# ============================================================
# API - MODELS
# ============================================================

@app.get("/api/models/status")
async def api_models_status():
    launcher = _get_launcher()
    if not launcher:
        return {"running": False, "models": []}
    status = launcher.get_status()
    return {
        "running": bool(status.local_running),
        "models": list(status.local_running.values()),
        "source": status.model_source
    }


@app.post("/api/models/kill")
async def api_models_kill():
    launcher = _get_launcher()
    if not launcher:
        return {"error": "launcher not available"}
    try:
        launcher.shutdown_all()
        return {"status": "killed"}
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# API - RUNS (orchestrator, Modalita' 1: Mantenimento)
# ============================================================

class RunRequest(BaseModel):
    path: str
    task: str = "trova e correggi eventuali bug"
    entrypoint: Optional[str] = None
    max_attempts: int = 3
    max_seconds: int = 300


@app.post("/api/run")
async def api_run(req: RunRequest):
    if not req.path:
        return {"error": "missing path"}

    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")

    def _bg():
        try:
            def sse_callback(msg, level):
                log_path = LOG_DIR / f"{run_id}.log"
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"[{level.upper()}] {msg}\n")

            with Orchestrator(
                config_path=CONFIG_PATH,
                project_path=req.path,
                sse_callback=sse_callback
            ) as orch:
                with runs_lock:
                    active_runs[run_id] = orch
                try:
                    result = orch.run(
                        task=req.task,
                        project_path=req.path,
                        entrypoint=req.entrypoint,
                        max_attempts=req.max_attempts,
                        max_seconds=req.max_seconds,
                        run_id=run_id
                    )
                    # FIX: niente piu' scrittura qui — orchestrator.run() scrive GIA'
                    # il footer 'status: X' internamente (in ogni return path, vedi
                    # write_status_footer() in orchestrator.py). Scriverlo anche qui
                    # duplicava la riga.
                finally:
                    with runs_lock:
                        active_runs.pop(run_id, None)
        except Exception as e:
            log_path = LOG_DIR / f"{run_id}.log"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[FATAL] {e}\n")
                f.write("status: failed\n")

    t = threading.Thread(target=_bg, daemon=True)
    t.start()

    return {"run_id": run_id, "status": "started"}


# ============================================================
# API - SCAFFOLD (Modalita' 2: Zero-Shot Scaffolding, "Chat First")
# ============================================================

@app.post("/api/chat/scaffold")
async def api_chat_scaffold(req: RunRequest):
    """
    Avvia la creazione di un progetto da zero, esclusivamente via tool (no diff pipeline).
    Il frontend fa subito subscribe a /stream/{run_id}: nessun tempo morto silenzioso,
    ogni file creato emette un evento SSE (regola Chat First).
    """
    if not req.path:
        return {"error": "missing path"}

    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{run_id}.log"
    log_path.write_text(f"Scaffold started: {run_id}\nTask: {req.task}\n", encoding="utf-8")

    def sse_callback(msg, level):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{level.upper()}] {msg}\n")

    def _bg():
        try:
            with Orchestrator(
                config_path=CONFIG_PATH,
                project_path=req.path,
                sse_callback=sse_callback
            ) as orch:
                with runs_lock:
                    active_runs[run_id] = orch
                try:
                    result = orch.run_scaffold(task=req.task, project_path=req.path, run_id=run_id)
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(f"\nstatus: {'success' if result.get('success') else 'failed'}\n")
                finally:
                    with runs_lock:
                        active_runs.pop(run_id, None)
        except Exception as e:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[FATAL] {e}\n")
                f.write("status: failed\n")

    t = threading.Thread(target=_bg, daemon=True)
    t.start()

    return {"run_id": run_id, "status": "started", "mode": "scaffold"}


@app.post("/api/stop")
async def api_stop(request: Request):
    data = await request.json()
    run_id = data.get("run_id")
    if not run_id:
        return {"error": "missing run_id"}

    with runs_lock:
        orch = active_runs.get(run_id)

    if orch:
        orch.stop()
        return {"status": "stop_requested", "run_id": run_id}
    return {"error": "run not found or already finished"}


# ============================================================
# API - LOGS
# ============================================================

@app.get("/api/runs/active")
async def api_runs_active():
    """
    Run realmente in esecuzione ORA (oggetti Orchestrator vivi in memoria), non una
    euristica sul contenuto del log. Un run vecchio/crashato che non ha scritto la
    riga finale 'status: ...' ha status='unknown' in /api/runs ma NON e' qui dentro:
    evita che la dashboard lo mostri come 'in esecuzione' per sempre.
    """
    with runs_lock:
        return {"active_run_ids": list(active_runs.keys())}


@app.get("/api/runs")
async def api_runs():
    if not LOG_DIR.exists():
        return []
    runs = []
    for f in sorted(LOG_DIR.glob("run_*.log"), reverse=True):
        stat = f.stat()
        content = f.read_text(encoding="utf-8", errors="ignore")
        status = "unknown"
        if "status: success" in content.lower():
            status = "success"
        elif "status: failed" in content.lower():
            status = "failed"
        elif "status: timeout" in content.lower():
            status = "timeout"
        elif "status: stopped" in content.lower():
            status = "stopped"
        runs.append({
            "run_id": f.stem,
            "file": str(f.name),
            "size": f.stat().st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "status": status,
            "preview": content[:500]
        })
    return runs[:50]


@app.get("/api/run/{run_id}/log")
async def api_run_log(run_id: str):
    log_path = LOG_DIR / f"{run_id}.log"
    if not log_path.exists():
        return {"error": "not found"}
    return {
        "run_id": run_id,
        "content": log_path.read_text(encoding="utf-8", errors="ignore")
    }


@app.get("/stream/{run_id}")
async def stream_log(run_id: str):
    log_path = LOG_DIR / f"{run_id}.log"

    async def generate():
        for _ in range(20):
            if log_path.exists():
                break
            await asyncio.sleep(0.5)
            yield f"data: {json.dumps({'type': 'wait', 'msg': 'Waiting for log file...'})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'msg': 'Log file not found'})}\n\n"
            return

        await asyncio.sleep(0.5)

        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.3)
                    continue
                payload = json.dumps({"type": "log", "line": line.rstrip("\n")})
                yield f"data: {payload}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )


# ============================================================
# API - AUTOCOMPLETE (Task 16, client condiviso — fix bug 1.2)
# ============================================================

class AutocompleteRequest(BaseModel):
    code: str


@app.post("/api/autocomplete")
async def api_autocomplete(req: AutocompleteRequest):
    if not req.code:
        return {"suggestion": ""}

    try:
        auto = _get_autocomplete()
        suggestion = auto.suggest(req.code)
        return {"suggestion": suggestion or ""}
    except Exception as e:
        return {"suggestion": "", "error": str(e)}


# ============================================================
# API - AUTOCOMPLETE STREAMING (Task 16 — Monaco Editor)
# ============================================================

class AutocompleteStreamRequest(BaseModel):
    code: str
    language: str = "python"
    cursor_position: int = None


@app.post("/api/autocomplete/stream")
async def api_autocomplete_stream(req: AutocompleteStreamRequest):
    """
    Autocomplete con streaming SSE per Monaco Editor.
    Usa il modello Coder locale (backup leggero) per suggerimenti rapidi.
    """
    if not req.code:
        return {"error": "empty code"}

    auto = _get_autocomplete()

    async def generate_sse():
        try:
            yield f"event: meta\ndata: {json.dumps({'language': req.language, 'mode': 'coder'})}\n\n"

            token_count = 0
            for chunk in auto.suggest_stream(req.code, language=req.language, cursor_position=req.cursor_position):
                token_count += 1
                yield f"data: {json.dumps({'token': chunk})}\n\n"
                await asyncio.sleep(0)

            yield f"event: done\ndata: {json.dumps({'tokens': token_count})}\n\n"

        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        generate_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# ============================================================
# MAIN + AUTO-OPEN
# ============================================================

def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except Exception:
        return False


def run_server():
    """
    Avvio completo del server (browser auto-open + uvicorn con shutdown pulito).
    Estratto in funzione richiamabile cosi' launcher.py puo' avviare QUESTA dashboard
    (non la vecchia UI Tkinter in devin/ui/app.py) con un semplice import + call.
    """
    import uvicorn

    URL = "http://localhost:5000"

    def open_browser():
        time.sleep(2)
        if _is_wsl():
            # L'interop WSL->Windows per aprire il browser (via rundll32.exe) e'
            # inaffidabile e scrive errori direttamente su stderr, non intercettabili
            # da un try/except Python. Piu' robusto saltarlo del tutto su WSL.
            print(f"\n-- WSL rilevato: apri manualmente {URL} nel browser")
            return
        print(f"\n-- Opening browser at {URL}")
        try:
            webbrowser.open(URL)
        except Exception as e:
            print(f"\n-- Impossibile aprire il browser automaticamente ({e}). Apri manualmente: {URL}")

    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()

    # FIX: le connessioni SSE aperte (/stream/{run_id}, polling ogni 5s da piu' tab)
    # impedivano allo shutdown grazioso di uvicorn di completarsi entro tempi ragionevoli
    # su Ctrl+C (richiedeva pkill). timeout_graceful_shutdown basso + os._exit di
    # sicurezza garantiscono che il processo termini sempre entro ~3s.
    config = uvicorn.Config(app, host="0.0.0.0", port=5000, log_level="info", timeout_graceful_shutdown=3)
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[SHUTDOWN] Chiusura forzata (i modelli locali/rig restano attivi in background)...")
        os._exit(0)


if __name__ == "__main__":
    run_server()
