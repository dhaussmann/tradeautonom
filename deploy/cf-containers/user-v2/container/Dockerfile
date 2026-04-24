# UserContainer-v2 image — Phase F.1.
#
# Same trading engine as V1 `tradeautonom:v3`, but built for Cloudflare
# Containers instead of Docker-on-Photon:
#   - Code is BAKED into the image (no shared-mount hot-reload). `wrangler
#     deploy` rebuilds and rolls out new instances atomically.
#   - `/app/data` is ephemeral on CF Containers. Phase F.2 adds R2 sync
#     (app/cloud_persistence.py) to restore state on cold start.
#   - Runs the real FastAPI surface from `app/server.py` unchanged — no
#     changes to the trading engine.
#
# Docker build context is the REPO ROOT (`image_build_context: "../../.."`
# in wrangler.jsonc) so that COPY can pick up app/, static/, main.py,
# requirements.txt directly from their canonical locations.
#
# Reference: deploy/prod/Dockerfile (V1), docs/v2-cf-containers-architecture.md

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps:
#   - gcc: needed for some pip packages (curl_cffi, eth-utils C exts).
#   - tini: reaps zombie forks from HEALTHCHECK Python subprocesses
#     (same as V1 — see docs/container-init-zombies.md).
#   - curl + ca-certificates: HEALTHCHECK + general outbound TLS.
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc tini curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Python deps. The root requirements.txt is pinned to the same versions
# tradeautonom-v3 runs in V1 (verified in Phase F.0). F.4 persistence uses
# only urllib (stdlib) to call back to the user-v2 Worker — no boto3/S3
# client needed since the Worker owns the R2 binding.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# App code. V2 is HEADLESS — no static/ directory, no /ui route. The
# user-facing Vue SPA lives on bot.defitool.de (see deploy/cloudflare/).
# app/server.py skips the /ui registration when V2_HEADLESS=1.
COPY app/ /app/app/
COPY main.py /app/main.py

# Phase F.2 entrypoint: restores /app/data from R2 on cold start (no-op on V1).
COPY deploy/cf-containers/user-v2/container/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Signal to app/server.py to skip the /ui static-file endpoint.
ENV V2_HEADLESS=1

# Build cache-bust marker for Phase F.4 roll-out
ENV V2_BUILD_TAG=f4-2026-04-24-v9-bodydiag

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fs "http://localhost:${APP_PORT:-8000}/health" || exit 1

# tini as PID 1 for proper child-process reaping.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/entrypoint.sh"]
