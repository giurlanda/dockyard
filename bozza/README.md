# Claude Code Launcher

Web app che, da un URL GitHub o GitLab, clona il repository e avvia un container dedicato con
`claude remote-control`. Le sessioni si controllano dall'app Claude sul telefono (tab **Code**).

È uno **scheletro MVP**: la struttura e i flussi principali ci sono, ma non è stato provato su una
macchina reale. Parti dalla Fase 0 qui sotto prima di usarlo sul serio.

## Struttura

```
compose.yaml                stack: socket-proxy, launcher, cloudflared
.env.example                variabili da copiare in .env
project-image/Dockerfile    immagine base dei container di progetto
launcher/                   web app FastAPI
  app/main.py               API REST + WebSocket (terminale, console, log)
  app/docker_ops.py         clone, avvio, stop, statistiche, login Claude
  app/auth.py               verifica JWT di Cloudflare Access
  app/db.py                 SQLite + cifratura token (Fernet)
  app/templates/index.html  dashboard + xterm.js
host/block-lan.sh           regole iptables: progetti senza accesso a LAN e host
host/claude-launcher-firewall.service   le riapplica a ogni avvio
```

## Installazione su Ubuntu

1. **Docker**: installa Docker Engine e il plugin compose (guida ufficiale Docker per Ubuntu).
2. **Immagine dei progetti**
   ```
   docker build -t claude-project:latest ./project-image
   ```
3. **Configurazione**
   ```
   cp .env.example .env
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   Metti la chiave in `LAUNCHER_SECRET_KEY` e compila le voci Cloudflare (punto 5).
4. **Avvio**
   ```
   docker compose up -d --build
   ```
   Al primo avvio il launcher crea la rete `projects-net` (interfaccia `br-projects`) e il volume `claude-config`.
5. **Cloudflare**
   - Zero Trust → Networks → Tunnels: crea un tunnel, copia il token in `CLOUDFLARE_TUNNEL_TOKEN`,
     aggiungi un public hostname (es. `launcher.tuodominio.it`) verso `http://launcher:8000`.
   - Zero Trust → Access → Applications: crea un'app self-hosted sullo stesso hostname, policy
     "Allow" solo per la tua email. Copia l'**AUD tag** in `CF_ACCESS_AUD` e il team in `CF_ACCESS_TEAM`.
   - Imposta `ALLOWED_EMAIL` con la stessa email, poi `docker compose up -d`.
6. **Firewall dell'host** (dopo il punto 4, così la rete esiste)
   ```
   sudo cp host/block-lan.sh /usr/local/sbin/claude-block-lan.sh
   sudo chmod 755 /usr/local/sbin/claude-block-lan.sh
   sudo cp host/claude-launcher-firewall.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now claude-launcher-firewall
   ```
   Se usi un GitLab self-hosted in LAN, aggiungi il suo IP in `ALLOW_IPS` nel file .service
   e il suo host in `ALLOWED_GIT_HOSTS`.

## Primo utilizzo

1. Apri la dashboard e premi **Collega account Claude**: nel terminale apri il link, fai il login,
   incolla il codice. Chiudi la finestra.
2. Crea un progetto (URL, nome, token se privato).
3. Apri la **Console** del progetto e rispondi `y` a "Enable Remote Control?" e al trust della cartella.
   Non premere Ctrl+C nella console: ferma il server (Docker lo riavvia, ma perdi la sessione).
4. Sul telefono: app Claude → **Code** → la sessione con il nome del progetto.

Il push lo fai tu dal **Terminale** del progetto, quando vuoi, inserendo il token in quel momento.

## Fase 0: cose da verificare prima di tutto

- [ ] **Config condivisa**: due o tre progetti attivi sullo stesso volume `claude-config` per 48 ore.
      Se il login salta o le sessioni si scollegano, passa a un volume per progetto copiato alla creazione.
- [ ] **Conferme iniziali**: che bastino una volta (globale) o vadano date per ogni cartella.
- [ ] **Socket proxy**: che terminale, console e log (connessioni "hijacked") passino da `docker-socket-proxy`.
- [ ] **Stato login**: `claude_logged_in()` controlla `.credentials.json` nel volume; verifica il nome del file.
- [ ] **Modalità permessi**: che `--permission-mode` accetti `auto` e `bypassPermissions`
      (se il valore è errato il server si ferma ed elenca quelli validi: vedi Log).
- [ ] **Firewall**: da un terminale di progetto, `curl -m 3 http://<IP-del-router>` deve fallire,
      `curl -I https://github.com` deve funzionare.

## Note di sicurezza

- Il launcher non vede `docker.sock`: parla solo con il socket proxy, su una rete interna.
- I container di progetto girano come utente non-root, senza capability, senza accesso a LAN, host
  o agli altri progetti.
- Il token Git compare solo nelle variabili del container di clone, che viene eliminato subito dopo;
  il remote `origin` resta senza credenziali.
- Il volume `claude-config` è visibile a tutti i progetti: usa `bypassPermissions` solo su repo fidati.
- Non impostare `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` o `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`
  nei container di progetto: bloccano Remote Control.

## Sviluppo locale

Con `DEV_NO_AUTH=1` la verifica Cloudflare è disattivata. Pubblica la porta solo su localhost
(es. `ports: ["127.0.0.1:8000:8000"]` in un `compose.override.yaml`) e mai in produzione.
