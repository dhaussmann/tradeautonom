# ---- TradeAutonom Docker Image ----
# Dependencies-only image. App code is volume-mounted at runtime
# so code changes only require a container restart, not a rebuild.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies (gcc needed for some pip packages)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies (only layer that triggers rebuild)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code is NOT copied — it will be bind-mounted via docker-compose.
# This means code changes only need a container restart, not a rebuild.
# Only rebuild when requirements.txt changes.

EXPOSE 8000

CMD ["python", "main.py"]
