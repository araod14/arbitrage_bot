# Imagen para desplegar el dashboard web + el bot de monitoreo.
# El contenedor principal corre el DASHBOARD, que arranca/para el bot como
# subproceso. El bot sigue disponible como `python -m p2p_arb_bot.main` para
# uso por CLI directo (p. ej. `docker compose run`).
FROM python:3.12-slim

# - PYTHONUNBUFFERED: logs salen al instante (importante con `docker logs`).
# - PYTHONDONTWRITEBYTECODE: no ensucia con .pyc.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/opportunities.db \
    LOG_PATH=/data/p2p_arb_bot.log \
    STATUS_PATH=/data/status.json \
    BOT_PIDFILE=/data/bot.pid \
    ENV_PATH=/app/.env \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8000 \
    NO_INPUT=true

WORKDIR /app

# Instala dependencias primero para aprovechar la caché de capas de Docker.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Luego copia el código e instala el paquete (deps ya resueltas).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# Usuario no-root y directorio de datos persistente (BD + logs + estado).
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /data \
    && chown -R bot:bot /data /app
USER bot

VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "-m", "p2p_arb_bot.web"]
