#!/usr/bin/env bash
# UserContainer-v2 entrypoint.
#
# On cold start we restore /app/data/ from R2 BEFORE booting the FastAPI
# app. `app.cloud_persistence.restore_sync()` is a no-op when the
# V2_CLOUD_PERSISTENCE flag is unset (V1) or when the R2 credentials are
# incomplete — so running entrypoint.sh on V1 is harmless.
set -e

# Pull prior state from R2 if configured. Errors log but don't block boot.
python -c "from app.cloud_persistence import restore_sync; restore_sync()" || true

# Hand off to main.py (tini -> uvicorn -> FastAPI).
exec python main.py
