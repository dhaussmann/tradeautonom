# Phase F.0 feasibility PoC for UserContainer-v2.
#
# Goal: prove that the Python dependencies V1 uses (x10-python-trading-starknet,
# curl_cffi, fastapi/uvicorn/websockets, eth_account, grvt-pysdk) install and run
# under Cloudflare Containers' Linux userspace, and that the container can reach
# the trading exchanges and our OMS-v2 over WebSocket.
#
# This is NOT the production image. The production image (Phase F.1) will COPY
# the full app/ tree. Here we only ship a tiny FastAPI with feasibility probes.
#
# Reference: deploy/prod/Dockerfile (V1 production image)

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# gcc needed for some pip packages (curl_cffi, eth-utils C exts).
# tini avoids zombie accumulation from Docker HEALTHCHECK forks (same rationale
# as V1 prod image — see docs/container-init-zombies.md).
# curl + ca-certificates used by HEALTHCHECK and general outbound TLS.
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc tini curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install the EXACT V1 requirements (root requirements.txt).
# If any of these fail, F.0 has surfaced a real blocker.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# PoC-only test app.
COPY poc_main.py .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fs http://localhost:8000/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "poc_main.py"]
