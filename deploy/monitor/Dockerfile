FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy main service and all Nado watchdog modules
COPY monitor_service.py .
COPY nado_watchdog.py .
COPY nado_persistence.py .
COPY nado_discovery.py .
COPY nado_websocket.py .
COPY nado_config.yaml .

# Create data directory for persistence
RUN mkdir -p /app/data

EXPOSE 8099

CMD ["uvicorn", "monitor_service:app", "--host", "0.0.0.0", "--port", "8099"]
