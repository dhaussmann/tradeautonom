# ---- TradeAutonom NAS Image ----
# Self-contained image with code baked in.
# Only .env and data/ are mounted as volumes on the NAS.
# Rebuild after code changes: ./deploy.sh
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies.
# - gcc needed for some pip packages.
# - tini is a lightweight init process that properly reaps child processes.
#   Without it, Docker HEALTHCHECK-spawned Python forks become zombies
#   (PID 1 is main.py, which has no SIGCHLD handler). 500+ zombies per
#   container per day caused OOM-kills on the 512 MiB cgroup in production.
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc tini && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY static/ ./static/
COPY main.py .

# data/ is mounted as a volume (trade logs persist across rebuilds)
VOLUME /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://localhost:'+os.environ.get('APP_PORT','8000')+'/health')" || exit 1

# tini as PID 1 — reaps HEALTHCHECK zombies and forwards signals correctly.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "main.py"]
