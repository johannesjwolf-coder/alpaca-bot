# Betrieb auf dem Server (Hetzner + Tailscale + ntfy)

Diese Anleitung bringt den Bot auf einen eigenen Linux-Server, damit er rund um die Uhr läuft – auch wenn Ihr PC aus ist.

**Ergebnis:**

* Der Bot läuft im Docker-Container auf dem Server und startet nach einem Absturz oder Server-Neustart von selbst.
* Die Oberfläche erreichen Sie **nur über Tailscale**, ein privates, verschlüsseltes Netz Ihrer eigenen Geräte. Adresse: `https://alpaca-bot.<ihr-tailnet>.ts.net`. Aus dem Internet ist sie nicht erreichbar.
* Push-Nachrichten aufs Handy kommen über die App **ntfy**: Trades, Warnungen, Bot gestartet/pausiert, Tageszusammenfassung.
* Kurse und Order-Ausführungen kommen per **Echtzeit-Datenstrom** von Alpaca; fällt er aus, fragt der Bot automatisch alle 2 Sekunden ab.
* Vom Internet aus ist nur SSH (Port 22) offen. Updates und Backups laufen mit je einem Befehl, Backups zusätzlich täglich automatisch.

Weiterhin gilt: **nur Paper Trading**. Der Server spricht ausschliesslich mit `paper-api.alpaca.markets`, `data.alpaca.markets` und `stream.data.alpaca.markets` (Echtzeit-Kurse), dazu für die Push-Nachrichten mit `ntfy.sh`.

---

## Kosten

| Posten | Kosten |
| --- | --- |
| Hetzner Cloud CX23 (2 vCPU, 4 GB RAM) | laut Hetzner-Preisliste wenige Euro pro Monat |
| Optional: Hetzner-Backups des ganzen Servers | +20 % des Serverpreises |
| Tailscale (Personal) | kostenlos |
| ntfy über ntfy.sh | kostenlos |
| Alpaca Paper-Konto und IEX-/Krypto-Marktdaten | kostenlos |

---

## 1. Vorbereitung (einmalig, ca. 10 Minuten)

1. **Tailscale-Konto anlegen:** auf [tailscale.com](https://tailscale.com) „Get started“ wählen und mit Google, Microsoft, Apple oder GitHub anmelden.
2. **Tailscale-App installieren** auf dem Handy (App Store/Play Store: „Tailscale“) und auf dem PC, jeweils mit demselben Konto anmelden.
3. **HTTPS im Tailnet einschalten:** im Tailscale-Adminbereich unter **DNS** prüfen, dass *MagicDNS* an ist, und weiter unten bei *HTTPS Certificates* auf **Enable HTTPS** klicken. Schalten Sie es hier nicht ein, zeigt das Installationsskript später einen Link dafür.
4. **ntfy-App installieren** (App Store/Play Store: „ntfy“). Noch nichts abonnieren, das Thema erzeugt das Skript.
5. **Alpaca-Paper-Schlüssel bereithalten:** [app.alpaca.markets](https://app.alpaca.markets) → oben links auf das **Paper**-Konto umschalten → *API Keys*. Die Schlüssel tippen bzw. fügen Sie gleich **direkt auf dem Server** ein, nirgendwo sonst (nicht in den Chat).

---

## 2. Installation

**a) Mit dem Server verbinden.** Auf dem PC PowerShell öffnen. Die IP-Adresse steht in der Hetzner Cloud Console beim Server.

```bash
ssh root@IHRE-SERVER-IP
```

**b) Git installieren und den Code holen:**

```bash
apt-get update && apt-get install -y git
```

```bash
git clone -b claude/pensive-sagan-wjwgcp https://github.com/johannesjwolf-coder/alpaca-bot.git /opt/alpaca-bot
```

**c) Installationsskript starten.** Sie können es vorher mit `less /opt/alpaca-bot/deploy/install.sh` lesen.

```bash
bash /opt/alpaca-bot/deploy/install.sh
```

Während das Skript läuft, müssen Sie an drei Stellen etwas tun:

1. **Tailscale-Anmeldung:** Es erscheint `To authenticate, visit: https://login.tailscale.com/a/…`. Öffnen Sie den Link im Browser, melden Sie sich an und bestätigen Sie das Gerät. Das Skript läuft danach von selbst weiter.
2. **Alpaca-Schlüssel:** Fügen Sie *API Key ID* und *Secret Key* ein, in PowerShell per Rechtsklick, dann Enter. **Es wird absichtlich nichts angezeigt.** Gespeichert werden die Schlüssel nur in `/opt/alpaca-bot/.env`, die nur root lesen darf.
3. **Falls HTTPS im Tailnet noch aus ist:** Tailscale zeigt einen Link. Öffnen Sie ihn und bestätigen Sie.

Der erste Lauf dauert einige Minuten, weil Docker die Anwendung baut. Am Ende zeigt das Skript:

```text
✔  Fertig.
   Oberfläche:   https://alpaca-bot.tailxxxx.ts.net   (Tailscale auf Handy/PC einschalten)
   ntfy-Thema:   alpaca-bot-…
```

**d) Push-Nachrichten einrichten:** In der ntfy-App auf **+** tippen, das angezeigte Thema eingeben (Server `ntfy.sh` lassen) und **Abonnieren** wählen. Das Thema wie ein Passwort behandeln, denn wer es kennt, kann mitlesen.

**e) Oberfläche öffnen:** Auf dem Handy Tailscale einschalten und im Browser die angezeigte Adresse öffnen. Tipp: Über „Zum Home-Bildschirm“ wird daraus ein App-Symbol.

**f) Benachrichtigung testen:** In der Oberfläche *Einstellungen → Benachrichtigungen → Testnachricht senden*.

**g) Tailscale-Schlüsselablauf ausschalten:** Im Tailscale-Adminbereich unter *Machines* beim Gerät `alpaca-bot` auf **…** tippen und **Disable key expiry** wählen. Sonst verliert der Server nach etwa 180 Tagen die Verbindung zum Tailnet.

---

## 3. Bot auf dem Server starten

Die Anwendung startet wie immer im **DEMO**-Modus, mit ausgeschalteter Automatik und gesperrter Orderfreigabe.

1. *Einstellungen* → **PAPER**, dann **Paper-Orders freigeben…** und bestätigen.
2. Unter *Bot* die Scalping-Parameter prüfen. Standard: `BTC/USD`, `ETH/USD`, `SOL/USD`, 50 USD je Trade, Schutzorder beim Broker an. Danach **Bot starten**.
3. Optional unter *Einstellungen → Nach einem Neustart*: **Fortsetzen einschalten…** (Erklärung unten).

### Fortsetzen nach Neustart (optional, Standard: aus)

* **Aus (Standard):** Nach jedem Neustart, zum Beispiel nach einem Update, einem Server-Neustart oder einem Absturz, sind Automatik und Orderfreigabe aus. Sie bekommen eine Push-Nachricht und starten den Bot selbst wieder.
* **An:** Lief der Bot beim Neustart, stellt die Anwendung Modus, Orderfreigabe und Bot selbst wieder her. Gelingt das in 10 Versuchen (etwa 5 Minuten) nicht, wird die Freigabe aufgehoben und Sie bekommen eine Fehlermeldung. Haben Sie vorher selbst pausiert, die Freigabe aufgehoben oder Not-Aus ausgelöst, bleibt alles aus.

### Schutzorder beim Broker

Nach jedem Kauf legt der Bot beim Broker eine **Stop-Limit-Verkaufsorder** an: Stopp = Stop-Loss, Limit 0,5 % darunter. Sie greift auch dann, wenn Server oder Bot gerade nicht laufen. Fällt der Kurs so schnell, dass er unter dem Limit liegt, wird sie nicht ausgeführt; dann verkauft der Bot nach 20 Sekunden selbst. Bei Take-Profit oder Ablauf der Haltedauer storniert der Bot zuerst die Schutzorder und verkauft dann. „Position schliessen“ und Not-Aus stornieren sie ebenfalls zuerst.

---

## 4. Alltag

Alle Befehle per SSH auf dem Server ausführen.

**Update auf die neueste Version.** Vorher wird die Datenbank gesichert. Läuft die Automatik und ist „Fortsetzen“ aus, fragt das Skript nach.

```bash
sudo bash /opt/alpaca-bot/deploy/update.sh
```

**Protokoll ansehen** (Strg+C beendet die Anzeige, der Bot läuft weiter):

```bash
docker compose -f /opt/alpaca-bot/docker-compose.yml logs -f --tail=100
```

**Status des Containers:**

```bash
docker compose -f /opt/alpaca-bot/docker-compose.yml ps
```

**Verbindungstest zu Alpaca (nur lesend):**

```bash
docker compose -f /opt/alpaca-bot/docker-compose.yml exec alpaca-bot python -m app.connection_check
```

**Alpaca-Schlüssel ersetzen**, zum Beispiel nach dem Neu-Erzeugen bei Alpaca. Die Eingabe ist verdeckt, danach startet die Anwendung neu.

```bash
sudo bash /opt/alpaca-bot/deploy/set-keys.sh
```

**Neu starten.** Die Automatik ist danach aus, ausser „Fortsetzen“ ist an.

```bash
docker compose -f /opt/alpaca-bot/docker-compose.yml restart
```

**Anhalten.** Die Daten bleiben erhalten. Offene Positionen und Schutzorders beim Broker bleiben bestehen.

```bash
docker compose -f /opt/alpaca-bot/docker-compose.yml down
```

### Backups

* Automatisch jeden Tag um 03:17 Uhr nach `/var/backups/alpaca-bot/`, 14 Tage aufbewahrt. Gesichert werden Orders, Trades, Einstellungen und Ereignisse, **keine** Zugangsdaten.
* Manuell sichern:

```bash
sudo bash /opt/alpaca-bot/deploy/backup.sh
```

* Wiederherstellen (vorher wird der aktuelle Stand gesichert):

```bash
sudo bash /opt/alpaca-bot/deploy/restore.sh /var/backups/alpaca-bot/DATEINAME.sqlite3.gz
```

* Zusätzlich empfehlenswert: in der Hetzner Cloud Console beim Server **Backups** einschalten (tägliche Sicherung des ganzen Servers).

### Betriebssystem

Sicherheitsupdates installiert Ubuntu automatisch, ohne Neustart. Ab und zu (etwa monatlich) zusätzlich ausführen:

```bash
apt-get update && apt-get upgrade -y
```

Meldet Ubuntu beim Anmelden „System restart required“:

```bash
reboot
```

Der Bot startet danach von selbst, die Automatik nur bei eingeschaltetem „Fortsetzen“.

---

## 5. Sicherheit

* **Keine offene Weboberfläche:** Die Anwendung lauscht nur auf `127.0.0.1:8000` des Servers. Von aussen erreichbar ist sie nur über Tailscale (`tailscale serve`, HTTPS, nur Geräte in Ihrem Tailnet). Die Firewall (`ufw`) lässt eingehend nur SSH und Tailscale zu.
* **Keine Benutzeranmeldung in der App:** Wer in Ihrem Tailnet ist, kann sie bedienen. Teilen Sie das Tailnet nicht mit anderen Personen.
* **Schlüssel:** nur in `/opt/alpaca-bot/.env` (Rechte 600). Sie erscheinen weder in der Oberfläche noch in Logs, Backups oder Push-Nachrichten. Haben Sie einen Schlüssel versehentlich geteilt, erzeugen Sie ihn bei Alpaca neu und führen `set-keys.sh` aus.
* **ntfy-Thema** ändern, falls es bekannt geworden ist: `NTFY_TOPIC=` in `/opt/alpaca-bot/.env` mit `nano` anpassen, neu starten und in der App neu abonnieren.
* **Optional: SSH nur noch über Tailscale.** Erst prüfen, dass `ssh root@alpaca-bot` bei eingeschaltetem Tailscale auf dem PC funktioniert. Danach:

```bash
ufw delete allow OpenSSH
```

  Kommen Sie danach nicht mehr auf den Server, hilft die **Console** in der Hetzner Cloud Console (Notfallzugang über den Browser).

---

## 6. Fehlerbehebung

| Problem | Lösung |
| --- | --- |
| Seite lädt nicht | Tailscale auf dem Gerät eingeschaltet? Auf dem Server `tailscale status` und `tailscale serve status` prüfen. |
| „Invalid host header“ oder Aktionen schlagen mit 403 fehl | Tailscale-Name hat sich geändert. `bash /opt/alpaca-bot/deploy/install.sh` erneut ausführen; es trägt den aktuellen Namen ein. Die Schlüssel bleiben erhalten. |
| PAPER: „nicht autorisiert“ (401/403) | Schlüssel falsch oder erneuert → `set-keys.sh`. Nur **Paper**-Schlüssel verwenden. |
| *Diagnose*: Kursstrom „Verbindungslimit erreicht“ | Der kostenlose Alpaca-Zugang erlaubt einen Datenstrom je Konto. Läuft der Bot zusätzlich auf dem PC? Dort pausieren oder in dessen `.env` `APP_STREAMS=off` setzen. Bis dahin fragt der Server die Kurse ab. |
| *Diagnose*: Strom „Anmeldung abgelehnt“ | Schlüssel prüfen (`set-keys.sh`); der Bot fragt so lange ab. |
| Keine Push-Nachrichten | *Einstellungen → Benachrichtigungen* zeigt den letzten Fehler. Thema in der App muss genau dem Wert in `/opt/alpaca-bot/.env` entsprechen. |
| Build bricht mit „429 Too Many Requests“ ab | Docker Hub begrenzt anonyme Downloads. Eine Stunde warten oder mit kostenlosem Docker-Konto `docker login` ausführen und `update.sh --rebuild` starten. |
| Container startet immer wieder neu | Protokoll ansehen (siehe oben). Häufig: ungültige Werte in `.env`. |
