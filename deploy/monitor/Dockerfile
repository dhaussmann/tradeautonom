FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor_service.py .

EXPOSE 8099

CMD ["uvicorn", "monitor_service:app", "--host", "0.0.0.0", "--port", "8099"]
