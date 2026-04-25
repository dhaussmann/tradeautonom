# Phase F.4 M4 — Manual State Migration Tools

Bash-Scripts für **manuelle** State-Migration zwischen V1 (Photon Docker)
und V2 (Cloudflare Containers + R2). Diese Tools sind P0-Minimum für die
Thomas-Migration und werden von M5 (UI-Orchestrator) abgelöst.

## Voraussetzungen

- `~/.ssh/id_ed25519` für NAS-Zugang
- `.env` im Repo-Root mit:
  - `NAS_HOST=192.168.133.100`
  - `NAS_USER=root`
  - `INGEST_TOKEN=<...>` — für `/api/admin/probe/*`
  - `V2_SHARED_TOKEN=<...>` — für `/__state/*` Endpoints
- Lokal `curl`, `tar`, `python3`, `bash`

## Scripts

### `migrate-v1-to-v2.sh` — V1 → V2 (Photon nach CF)

Exportiert den `/app/data/` Snapshot eines V1-Users und schiebt ihn nach
R2, damit der V2-Container beim ersten Cold-Start den State restored.

```bash
# Dry-run: Snapshot lokal, kein Upload, zeigt Größe + Inhalt
./migrate-v1-to-v2.sh thomasolech --dry-run

# Upload nach R2 (kein D1-Flip, kein Photon-Stopp)
./migrate-v1-to-v2.sh thomasolech

# Upload + Anweisung zum manuellen D1-Flip
./migrate-v1-to-v2.sh thomasolech --flip

# Voll automatisch: Upload + (manuell) Flip + Photon stoppen
./migrate-v1-to-v2.sh thomasolech --flip --stop-photon
```

Die `--flip` Option prüft NICHT automatisch; sie zeigt nur den
`wrangler d1 execute`-Befehl an, mit dem du das D1-Update ausführen
kannst. Das automatische Flip-Endpoint braucht Session-Auth, nicht
INGEST_TOKEN.

**Schritte des Scripts:**

1. SSH-Connectivity-Check zur NAS
2. `/fn/bots` IDLE-Check für den User (refused wenn nicht alle IDLE/ERROR)
3. `docker exec ta-<user_id> tar czf - -C /app data` über SSH streamen
4. Repackage: Tarball wird umgepackt damit `/app/data/`-Inhalte direkt
   im Tarball-Root liegen (statt unter `data/`), und `_STATE_VERSION=1`
   wird hinzugefügt
5. POST nach `https://user-v2.defitool.de/__state/flush?user_id=<id>`
6. Verifikation: GET zurück per `/__state/restore`, Größenvergleich,
   `_STATE_VERSION`-Marker check
7. Optional: Anweisung für D1-Flip
8. Optional: `docker stop ta-<user_id>` auf NAS

### `migrate-v2-to-v1.sh` — V2 → V1 (Rollback)

Inverse Richtung. Holt R2-Tar, extrahiert in den Photon-Container.

```bash
# Dry-run
./migrate-v2-to-v1.sh thomasolech --dry-run

# Wiederherstellen + Anweisung zum D1-Flip
./migrate-v2-to-v1.sh thomasolech --flip
```

**Schritte:**

1. V2-IDLE-Check
2. Force-Flush auf V2 damit R2 aktuell ist
3. Tarball aus R2 herunterladen
4. Photon-Container `ta-<user_id>` muss existieren (sonst manuell
   neu erstellen via `deploy/v3/manage.sh create <user_id>`)
5. Photon-Container stoppen (falls running)
6. `/app/data` im Container löschen + Tar extrahieren (ohne
   `_STATE_VERSION`, weil V1 das nicht erwartet)
7. Photon-Container starten
8. Optional: D1-Flip Anweisung

## Bekannte Limitationen (M4)

- **Kein automatischer D1-Flip.** Das `/api/admin/user/.../backend`
  Endpoint braucht eine Admin-Session (Cookie). Manuelle Erledigung via
  Admin-UI oder direktes `wrangler d1 execute`. M5 wird ein
  Admin-Endpoint einführen, der den Flip + Migration in einem Schritt
  orchestriert.
- **Keine Quiesce-Pause.** Wenn Bots während des Snapshots schreiben,
  kann das Tarball inkonsistent sein. Das Script vertraut darauf, dass
  Step 1 alle Bots bereits IDLE hat.
- **Keine Rollback-Automatik bei Fehler.** Wenn Schritt 5 fehlschlägt,
  muss manuell aufgeräumt werden (Photon-Container starten, R2-Object
  ggf. löschen).
- **Container-Existenz wird nicht erstellt.** `migrate-v2-to-v1.sh`
  weigert sich, wenn der Photon-Container nicht existiert. User muss
  ihn vorher mit `deploy/v3/manage.sh create <user_id>` anlegen.

## Logs / Artefakte

Beide Scripts schreiben in `/tmp/ta-migrate-XXXXXX/` bzw.
`/tmp/ta-rollback-XXXXXX/`. Bei Fehler bleiben die Tarballs für
Inspektion liegen — am Ende manuell mit `rm -rf` aufräumen.
