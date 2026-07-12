# Rete del Pi — MAC, IP fisso sul Fastgate, watchdog

## Vedere il MAC address via SSH

```bash
# Tutte le interfacce, con MAC, in formato leggibile:
ip link show

# Solo il MAC dell'interfaccia che stai usando ora (quella con l'IP attivo):
ip -o link show $(ip route get 1.1.1.1 | grep -oP 'dev \K\S+') | grep -oP 'ether \K\S+'

# In alternativa, MAC di un'interfaccia specifica (es. eth0 o wlan0):
cat /sys/class/net/eth0/address
cat /sys/class/net/wlan0/address
```

⚠️ Se il Pi è collegato via **WiFi**, il MAC che conta è quello di `wlan0`;
via **cavo**, quello di `eth0`. Se un domani cambi da WiFi a cavo, la
prenotazione sul router va rifatta col MAC nuovo (sono due schede diverse).

## Prenotazione DHCP sul Fastgate (consigliata)

Procedura (interfaccia Fastgate standard — i nomi possono variare leggermente
tra firmware):

1. Browser → `http://192.168.1.254` (o `http://myfastgate.fastweb.it`)
2. Login (credenziali sotto il router se mai cambiate)
3. **Avanzate** (in alto) → **Impostazioni LAN** (menu a sinistra)
4. Scorri fino a **Associazioni DHCP** → **Aggiungi associazione DHCP**
5. Se c'è l'opzione, scegli **Aggiunta manuale**, poi:
   - **Indirizzo MAC**: quello del Pi (comando sopra)
   - **Indirizzo IP**: quello che vuoi fissare, es. `192.168.1.50`
6. **Aggiungi** / **Salva modifiche**

Da quel momento il Pi riceverà sempre quell'IP, anche dopo riavvii/reinstall.

Consiglio: già che ci sei, fai la stessa prenotazione anche per il **rig**
(MAC della sua NIC → `192.168.1.100`). Il rig ha comunque l'IP statico
impostato da noi lato OS, ma la prenotazione evita che il Fastgate assegni
il .100 a qualcun altro mentre il rig è spento.

## Alternativa: IP statico impostato SUL Pi (senza toccare il router)

Funziona uguale. Unico rischio: se un giorno il DHCP del Fastgate assegna
quell'IP a un altro dispositivo, hai un conflitto — per questo la prenotazione
sul router è preferibile. Ma come soluzione è del tutto legittima.

**Raspberry Pi OS Bookworm (2023+, usa NetworkManager):**
```bash
# via cavo (connessione di solito chiamata "Wired connection 1"):
sudo nmcli con mod "Wired connection 1" \
    ipv4.addresses 192.168.1.50/24 \
    ipv4.gateway 192.168.1.254 \
    ipv4.dns "8.8.8.8 192.168.1.254" \
    ipv4.method manual
sudo nmcli con up "Wired connection 1"
# (per WiFi: sostituisci col nome della tua connessione, vedi 'nmcli con show')
```

**Raspberry Pi OS Bullseye o precedenti (usa dhcpcd):**
```bash
sudo tee -a /etc/dhcpcd.conf > /dev/null << 'EOF'
interface eth0
static ip_address=192.168.1.50/24
static routers=192.168.1.254
static domain_name_servers=8.8.8.8 192.168.1.254
EOF
sudo systemctl restart dhcpcd
```
(Per WiFi: `interface wlan0` al posto di eth0.)

Per capire quale hai: `nmcli --version` funziona → Bookworm/NetworkManager;
"command not found" → Bullseye/dhcpcd.

## Watchdog di rete (NON parte da solo: va installato con questo comando)

Non fa parte di nessuno script del progetto — è un cron da aggiungere una volta:

```bash
( sudo crontab -l 2>/dev/null; echo '*/5 * * * * ping -c1 192.168.1.254 >/dev/null 2>&1 || systemctl restart networking' ) | sudo crontab -
```

Verifica che ci sia: `sudo crontab -l`
Rimozione futura: `sudo crontab -e` e cancella la riga.

Nota: su Bookworm `networking.service` può non esistere; in quel caso usa
`systemctl restart NetworkManager` nella riga del cron.

## Test WOL — requisito che conta

Il pacchetto WOL è un **broadcast di livello 2**: funziona solo se Pi e rig
sono attaccati alla **stessa rete locale** (stesso router/switch). NON
attraversa reti diverse, VPN, o internet.

Quindi: puoi testarlo tranquillamente anche sulla rete dove sei ORA, purché
Pi e rig siano collegati **entrambi lì**. Non serve che sia la rete definitiva
— il WOL se ne infischia degli IP, guarda solo il MAC.

```bash
wakeonlan AA:BB:CC:DD:EE:FF   # MAC della NIC del rig
```

Prerequisiti lato rig (BIOS MSI X299): Wake Up Event Setup → Resume By PCI-E
Device = Enabled, e **ErP/EuP = Disabled** (è il killer classico del WOL:
se attivo, taglia la corrente alla NIC a PC spento).
