# Fix: User-Container (9001-9003) Code & Secrets

This document describes the known issue where user containers (ports 9001-9003) run stale code from the Docker image and may lose vault data on rebuild.

---

## Problem Summary

| Issue | Affected | Root Cause |
|-------|----------|------------|
| **Stale code** | All user containers (9001-9003) | Code baked into Docker image, not shared volume |
| **Missing secrets** | Per-user vault (secrets.enc, auth.json) | Volume data lost or never created after rebuild |
| **Hot-deploy blind spot** | `./deploy.sh` only reaches port 8005 | User containers don't mount the shared `/app/` volume |

### Why It Happens

The main container (`tradeautonom-v3`, port 8005) runs code from the Docker image but **also** has a shared volume mount that hot-deploy (`./deploy.sh`) can update. User containers created via `manage.sh` **only** mount `/app/data` for persistence — they do **not** mount the shared code directory. Therefore:

- `./deploy.sh` updates files on the NAS → only 8005 picks them up via auto-reload
- User containers keep running the code that was baked into the image at build time
- When `manage.sh update` rebuilds and restarts, it recreates containers from the new image — but vault data (`secrets.enc`, `auth.json`) may be lost if the volume mount doesn't match

---

## Fix Instructions

### Step 1: Rebuild Image + Restart User Containers

This ensures all user containers run the latest code.

```bash
# From your dev machine (project root)
./deploy/v3/manage.sh update
```

This will:
1. Sync code to NAS
2. Rebuild the `tradeautonom:v3` Docker image
3. Stop, remove, and recreate all user containers with the new image
4. Data volumes (`/app/data`) are preserved (mount path unchanged)

**⚠ If SSH is not available**, do this manually via Synology Container Manager:
1. Open Container Manager on the NAS
2. Go to **Image** → find `tradeautonom:v3` → **Rebuild** (or delete and reimport)
3. For each user container (ta-9001, ta-9002, ta-9003):
   - Stop the container
   - Delete the container
   - Recreate with the same settings (port, volume mount, env file)

### Step 2: Users Must Re-Enter Vault Data

After the container restart, if `secrets.enc` was lost:

1. User opens their dashboard (e.g., `https://bot.defitool.de`)
2. If prompted: **Set a new vault password** (this creates `auth.json`)
3. Go to **Settings** → enter all API keys:
   - **Variational**: JWT token
   - **NADO**: Private key → then click **Authorize Bot** to generate linked signer
   - **Extended**: API key, public key, private key, vault ID
   - **GRVT**: API key, private key, trading account ID
4. Vault auto-saves to `secrets.enc` — persists across restarts

### Step 3: Verify Code Version

On each container, verify the code matches your local version:

```bash
# Local hashes
md5 -r app/nado_client.py app/engine.py app/server.py

# On each container (via Synology Docker UI → Terminal)
md5sum /app/app/nado_client.py /app/app/engine.py /app/app/server.py
```

All hashes must match.

### Step 4: Verify Vault Data Exists

On each user container:

```bash
ls -la /app/data/auth.json /app/data/secrets.enc
```

Both files must exist. If missing, the user needs to re-enter their data (Step 2).

---

## Permanent Fix: Shared Code Volume

To make hot-deploy (`./deploy.sh`) work for user containers, add a shared code volume mount to `manage.sh`.

### Changes Required in `deploy/v3/manage.sh`

#### In `cmd_create()` (line ~161):

Change:
```bash
ssh_nas "${P}/docker run -d \
    --name ${container_name} \
    --restart unless-stopped \
    -p ${port}:${port} \
    --env-file '${data_dir}/.env' \
    -v '${data_dir}/app-data:/app/data' \
    ${IMAGE_NAME}:${IMAGE_TAG}"
```

To:
```bash
ssh_nas "${P}/docker run -d \
    --name ${container_name} \
    --restart unless-stopped \
    -p ${port}:${port} \
    --env-file '${data_dir}/.env' \
    -v '${data_dir}/app-data:/app/data' \
    -v '${NAS_DEPLOY_PATH}/app:/app/app' \
    ${IMAGE_NAME}:${IMAGE_TAG}"
```

#### In `cmd_update()` (line ~304):

Apply the same change — add `-v '${NAS_DEPLOY_PATH}/app:/app/app'` to the `docker run` command.

### After Applying

1. Run `./deploy/v3/manage.sh update` to recreate containers with the new mount
2. Future `./deploy.sh` hot-deploys will automatically reach all containers
3. Verify: change a Python file → hot-deploy → check logs on 9001 for Uvicorn reload

### Important Notes

- `${NAS_DEPLOY_PATH}` resolves to `/volume1/docker/tradeautonom`
- The shared code path is `/volume1/docker/tradeautonom/app/`
- This is the same directory that `./deploy.sh` pushes files to
- The volume mount **overlays** the baked-in `/app/app/` directory from the Docker image
- Uvicorn's `--reload` (configured in `main.py`) watches `/app/app/` and will auto-restart

---

## Verification Checklist

After completing all steps:

- [ ] All containers show same md5 hashes for key files
- [ ] `auth.json` and `secrets.enc` exist in each container's `/app/data/`
- [ ] Users can log in, vault unlocks successfully
- [ ] Bots can be created, started, and trades execute normally
- [ ] `./deploy.sh` triggers Uvicorn reload on ALL containers (8005 + 9001-9003)
- [ ] Container logs show no "Signature does not match" or "Vault is locked" errors
