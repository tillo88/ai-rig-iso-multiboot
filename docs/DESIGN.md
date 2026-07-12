# Decisioni di design e storia del progetto

Questo file spiega i **perché**. Per il **come si usa**, vai al `README.md`.

## Il bug critico dello script originale v3.7.7 (P0, confermato)

Il late-command faceva il purge di `cloud-init` dentro `/target` **prima del reboot**.
Ma tutto il resto (script di setup, chiave SSH del bot, servizi systemd) era scritto
sotto `autoinstall.user-data.write_files` / `runcmd` — e quella sezione viene eseguita
da cloud-init **al primo avvio del sistema installato**, non durante l'installazione.
Risultato: cloud-init non c'era più al primo boot → nessuno di quei file/servizi veniva
mai creato. La ISO produceva un Ubuntu pulito ma nessuna delle funzionalità "AI rig".

**Fix scelto**: nessuna dipendenza da cloud-init dopo l'install. Tutto (copia script,
systemd unit, chiave SSH, enable servizi) avviene in `late-commands` via
`curtin in-target`, che gira garantito durante l'unica passata unattended.

## Altri bug corretti rispetto alla v3.7.7

| # | Problema originale | Fix |
|---|---|---|
| P0 | Disco hardcoded `/dev/sda` | `storage.match.serial` da `config/disks.env` |
| P0 | Stesso IP statico su 8 interfacce in parallelo | Install in DHCP; IP statico post-boot agganciato al **MAC** |
| P0 | `nvidia-smi` chiamato subito dopo `--dkms`, senza reboot | Stage separati: blacklist nouveau → install → reboot → poi CUDA |
| P0 | utente/host `qwen`/`ai-rig` | `tillo`/`tilloGPT-<ruolo>` |
| P1 | Cache montata tentando `/dev/sr0`, `/dev/sdb1`... a caso | Copiata da `/cdrom/cache` in `late-commands`, deterministico |
| P1 | Nessuna idempotenza tra stage | Marker `/var/lib/ai-rig/stage-*-done` + `ConditionPathExists` |
| P1 | `requirements.txt` per pacchetti OS | `packages/*.apt` per APT, `requirements/*.txt` per venv Python |

## Architettura: 3 install "clean" invece di clonare il rootfs

Alternativa considerata: installare una volta su DEVIN, poi `rsync` il rootfs su
HERMES/TEACHER rigenerando fstab/UUID/machine-id/hostname. Scartata perché:

- Zero rischio di `machine-id` duplicato o SSH host-key clonate (conflitti di
  identità di rete se due dischi finissero online insieme per errore).
- Zero rigenerazione manuale di fstab/UUID (fonte classica di boot rotti).
- Costo: ~5-8 min extra a disco per driver+CUDA. llama.cpp NON si ricompila 3
  volte: pre-compilato una volta sulla build machine, copiato via cache.

## Scelta llama.cpp vs vLLM

vLLM scartato per questo hardware: tensor-parallel simmetrico (fette uguali,
dimensionate sulla GPU più piccola — spreco enorme con VRAM 6-11GB miste),
`--tensor-parallel-size` vuole potenze di 2 (7 GPU non ci stanno), supporto
Pascal in declino. `--tensor-split` di llama.cpp pesa le fette in proporzione
alla VRAM reale di ogni scheda: nato per questo caso.

## BeeLlama.cpp: perché è il default e regole di reversibilità

Fork di llama.cpp (Anbeeld) con KV cache TurboQuant/TCQ (~7.5x vs ~2x di q8_0),
DFlash, reasoning-loop guard. Due categorie di feature con reversibilità opposta:

- **Cache type turbo**: runtime puro, i `.gguf` non cambiano. Reversibile.
- **Formati peso TQ3_1S/TQ4_1S** via `llama-quantize`: riscrivono il GGUF,
  mainline non li apre più. **Sola andata. Il progetto non li usa mai.**

Rischio: fork mantenuto da una persona. Mitigazione: `swap-llama-flavor.sh`
torna a mainline in un comando, e `LLAMA_FLAVOR` in `config/rig.env` permette
di ricostruire la ISO su mainline in qualsiasi momento.

⚠️ I cache turbo richiedono Flash Attention; benchmark upstream su Ampere
(RTX 3090). Le 3 GPU Pascal del rig vanno **misurate** (`bench-kv-cache.sh`)
prima di fidarsi del guadagno.

## DFlash / DSpark: perché non ora

Speculative decoding con drafter a diffusione (fino a 6x, lossless). MA il
drafter va **allenato in coppia** col modello target: oggi nessuno dei 3 modelli
del rig (Ornith, Deckard, Qwen3-VL) ne ha uno pubblicato. Su modelli
finetunati/merged, un drafter della base funzionerebbe (la verifica è lossless
per costruzione) ma con accelerazione degradata. Il supporto è già cablato:
drafter in `/opt/models/<ruolo>/dflash-drafter.gguf` → rilevato al riavvio.

## Modelli scelti e motivazioni

| Ruolo | Modello | Perché |
|---|---|---|
| Devin | Ornith-1.0-35B-A3B (MoE, 20.7GB) | Coding-agent, MoE = veloce (attiva ~3B/token) |
| Teacher | Qwen3-VL-30B-A3B-Thinking (MoE, 25.3GB + mmproj) | Vision + reasoning nativi |
| Hermes | DavidAU Deckard 40B **denso** Q6_K (~32GB + mmproj) | Personalità/qualità; denso = più lento, accettato |

Hermes-4.3-ABLITERATED scartato: text-only, e vision era un requisito.
Q6 minimo per tool-calling (raccomandazione del creatore, che vale per tutti e tre).

## Hardware: punti d'attenzione

- **32GB RAM** sono lo stress point per contesto lungo + servizi: context
  conservativi in `config/roles/*.env`, salire solo misurando (`free -h`).
- **Pascal (sm_61)** è a fine corsa: driver 5xx/CUDA 12.x pinnati in
  `config/rig.env`, non aggiornare senza verificare la matrice NVIDIA.
- **GPU parziali** (riser in arrivo): `30-gpu-detect.sh` ricalcola tensor-split
  a ogni boot; Hermes/Teacher in crash-loop finché la VRAM non basta è atteso
  e innocuo.

## AutoMem e llama.cpp: la correzione al riassunto di Gemini

llama.cpp **non parla MCP**. Il bridge `mcp-automem` serve per client MCP-aware —
Hermes-Agent lo è, quindi l'integrazione passa da lì. Devin/Teacher, senza
agent framework, possono comunque usare l'API REST di AutoMem direttamente
(`http://localhost:8001`).

## Web privato per Hermes: correzioni alla guida esterna

La guida SearXNG+Tor allegata in chat era giusta nell'impostazione, con 3 errori:
porta 8080 in conflitto col llama-server (→ 8081); "usa BeautifulSoup/Playwright
per l'estrazione" non corrisponde a Hermes-Agent (SearXNG è search-only,
`web_extract` richiede un provider dedicato → firecrawl-simple self-hosted);
mancava `hermes tools enable web`. Su Tor: il rischio vero non è la lentezza,
è che Google/Bing bloccano gli exit-node con CAPTCHA.
