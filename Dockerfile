# Einfaches Produktionsimage: Frontend bauen, Backend mit gesperrten Abhängigkeiten.
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    APP_ALLOW_NONLOCAL_BIND=1 \
    APP_DB_PATH=/data/alpaca-bot.sqlite3 \
    APP_FRONTEND_DIST=/app/frontend/dist
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY backend/app ./app
COPY --from=ui /ui/dist /app/frontend/dist
RUN useradd --system --uid 10001 bot && mkdir -p /data && chown bot /data
USER bot
EXPOSE 8000
# APP_HOST=0.0.0.0 gilt nur INNERHALB des Containers. docker-compose.yml bindet
# den Port auf dem Host ausschliesslich an 127.0.0.1.
CMD ["python", "-m", "app"]
