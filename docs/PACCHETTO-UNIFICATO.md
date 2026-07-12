# Pacchetto unificato — il rig e i suoi 3 client

Questo documento mette insieme tre progetti separati che condividono lo stesso rig fisico:
**ai-rig-iso-build** (questo progetto, fonte di verità per l'infrastruttura),
**devin_ai_ide** (client di coding, gira sulla workstation WSL2), e
**ForgeStudio** (client di automazione HueForge, gira su Windows). Non sono stati fusi in un
unico repo: restano tre progetti separati che si parlano via rete, con questo file come mappa
di come si incastrano. Non tocca `price_check_bot` (estraneo a questo lavoro).

## 1. Il rig fisico (specifiche corrette)

| | |
|---|---|
| CPU | Intel i9-10900X (10c/20t), scheda MSI X299 Pro |
| RAM | 32GB DDR4 3200 (espandibile a 64GB) |
| GPU | 2× GTX 1080 8GB, 1× GTX 1080 Ti 11GB, 1× RTX A2000 6GB, 2× GTX 1660 Super 6GB, 1× GTX 1660 Ti 6GB — **~51GB VRAM totale, 7 schede** |
| IP fisso | 192.168.1.100 (statico, DHCP solo in fase di install) |
| Accensione | Wake-on-LAN, gestito da bot Telegram su Raspberry Pi (`pi-bot/`) |

Nota hardware: solo la RTX A2000 (Ampere) ha Tensor Core reali. Le GTX 1660 Ti/Super montano il
die TU116 (Turing "castrato", senza Tensor Core) — si comportano come le Pascal (1080/1080Ti) sui
kernel a matrice, nonostante il nome suoni più recente.

Questa correzione (CPU i9-10900X, non i5-9600K) è stata applicata a
`devin_ai_ide/README_DEVIN_AI_IDE.md`, che riportava il dato sbagliato.

## 2. I 3 ruoli del rig

Il rig installa **3 sistemi Ubuntu 24.04 separati su 3 dischi** (uno per ruolo). È acceso un
disco/ruolo alla volta, stessa IP, stessa porta (`8080`) per il suo llama-server:

| Ruolo | Modello | Uso | Chi lo consuma |
|---|---|---|---|
| `devin` | Ornith-1.0-35B-A3B (MoE) | coding/debug agent | **devin_ai_ide** |
| `teacher` | Qwen3-VL-30B-A3B-Thinking (MoE, vision) | reasoning/vision per correggere un modello più piccolo | **ForgeStudio** (escalation teacher-student) |
| `hermes` | Qwen3.6-40B Deckard (denso, vision) + Hermes-Agent, AutoMem, ComfyUI, SearXNG | chat personale con memoria | uso diretto (non da questi due client) |

Cambio ruolo: bot Telegram sul Pi (`/devin` `/hermes` `/teacher`), o via SSH
`sudo grub-reboot <ruolo> && sudo reboot`.

Oltre al bot Pi (accensione/cambio ruolo), ogni ruolo ha un **bot dedicato** con
chat separata: `teacher-bot` (watchdog llama-server + alert quando ForgeStudio
gira da solo + `/restart` + `/ask` vision), `hermes-bot` (chat con memoria via
`hermes -z`), `devin-bot` (dashboard DEVIN). Girano sul disco del ruolo e partono
col ruolo. Setup e comandi: README §3.5b.

## 3. Vincolo operativo — un solo ruolo alla volta

Questo è il punto più importante da tenere a mente usando insieme i due client:

**devin_ai_ide** e **ForgeStudio** non possono usare il rig nello stesso momento**, perché
richiedono ruoli diversi (`devin` vs `teacher`) e il rig ne esegue uno solo per volta.
Se stai facendo debug pesante con DEVIN AI IDE (rig in ruolo `devin`) e in quel momento lanci
ForgeStudio, quest'ultimo non trova il teacher su `:8080` — restano solo lo studente locale
(UI-TARS) e la memoria accumulata finora. Viceversa, se il rig è in `teacher` per una sessione
HueForge, DEVIN AI IDE va in fallback sui modelli locali della RTX 5070 Ti (più lenti, non
raccomandati per scaffolding pesante — lo segnala già da solo in log).

Non è un bug: è la conseguenza diretta della scelta di architettura (fatta apposta, per isolare
dipendenze/librerie per ruolo). Va solo tenuto presente quando si pianifica una sessione di
lavoro: decidere prima quale dei due client serve, e mettere il rig sul ruolo giusto.

## 4. devin_ai_ide → ruolo `devin`

- Primario: rig (`192.168.1.100:8080`, Ornith, coding+reasoning nella stessa istanza).
- Fallback: modelli locali sulla workstation WSL2 (RTX 5070 Ti) — solo chat leggera e
  autocomplete, non scaffolding/debug pesante (`config/settings.json` lo segnala già:
  `local_role: "chat_and_autocomplete_only"`).
- Ricerca web in chat: TinyFish (cloud, già configurato) come provider di default. C'è anche
  un'opzione SearXNG self-hosted, ma **gira solo sul ruolo `hermes`** (porta 8081, non 8888 come
  scritto prima — corretto in `config/settings.json`), quindi non è raggiungibile durante le
  normali sessioni di coding col rig in ruolo `devin`. Resta comunque configurato per chi vuole
  passare a `hermes` apposta.

## 5. ForgeStudio → ruolo `teacher`

- Studente: UI-TARS locale (Windows, RTX 5070 Ti) via `llama-server` bundlato nel progetto.
- Insegnante (escalation su fallimento/incertezza): rig in ruolo `teacher`
  (`http://192.168.1.100:8080/v1`, Qwen3-VL-30B-A3B-Thinking).
- La cartella `rig/` che compariva nel README di ForgeStudio (script di build/hot-swap di una
  versione precedente del rig, quando era ancora un mining rig separato su `192.168.1.86` con
  Ollama) **non esiste più nel progetto** — il README è stato aggiornato per rimandare a
  `ai-rig-iso-build` come fonte di verità, invece di descrivere file non più presenti.

## 6. Cosa NON è stato toccato

Nessuna riscrittura di logica applicativa in devin_ai_ide o ForgeStudio: solo correzioni di
documentazione/config (specifiche hardware, porta SearXNG, riferimenti al vecchio rig) per
allineare tutto a quello che il rig fa davvero oggi. Il codice dei due agenti (loop, memoria,
test) resta quello che avete già validato nelle rispettive chat di sviluppo.

## 7. Aperto / da decidere

- **SearXNG raggiungibile da DEVIN**: oggi non c'è un modo per avere ricerca web privata mentre
  il rig è in ruolo `devin` (SearXNG vive solo su `hermes`). Valutato metterlo sul Raspberry Pi
  del bot (sempre acceso) — **scartato**: il Pi è un modello 1 B+ del 2014 (ARMv6, 512MB RAM),
  non regge Docker/SearXNG. Per ora resta così: TinyFish come default per DEVIN, SearXNG solo
  nelle sessioni `hermes`. Da rivedere se in futuro arriva un Pi più recente (4/5) o si dedica
  un'altra macchina sempre accesa.
- **4° disco condiviso** (AutoMem/backup) in ai-rig-iso-build: previsto ma non ancora installato,
  aggiungibile senza reinstallare nulla quando arriva.
- **Validazione hardware reale**: `docs/DESIGN.md` segnala che i cache-type "turbo" di beellama
  e le soglie di `ForgeStudio` (Delta E, SSIM, loop-escape) non sono ancora state misurate su
  hardware reale — prima esecuzione vera da fare quando il rig è operativo su tutti e 3 i ruoli.
