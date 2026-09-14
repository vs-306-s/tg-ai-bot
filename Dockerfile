# Образ для хостинга (Railway, Render, Fly.io, любой VPS с Docker)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# База и настройки живут в /data — подключи к этому пути постоянный диск (volume)
VOLUME ["/data"]

CMD ["python", "main.py"]
