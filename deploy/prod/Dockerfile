# ---- TradeAutonom NAS Image ----
# Self-contained image with code baked in.
# Only .env and data/ are mounted as volumes on the NAS.
# Rebuild after code changes: ./deploy.sh
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies (gcc needed for some pip packages)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
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

CMD ["python", "main.py"]
