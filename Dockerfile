# Imagen para desplegar el bot de monitoreo en modo no interactivo.
# El bot por defecto es interactivo; en contenedor se ejecuta con NO_INPUT=true
# y la configuración se toma de variables de entorno / .env.
FROM python:3.12-slim

# - PYTHONUNBUFFERED: logs salen al instante (importante con `docker logs`).
# - PYTHONDONTWRITEBYTECODE: no ensucia con .pyc.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/opportunities.db \
    LOG_PATH=/data/p2p_arb_bot.log \
    NO_INPUT=true

WORKDIR /app

# Instala dependencias primero para aprovechar la caché de capas de Docker.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Luego copia el código e instala el paquete (deps ya resueltas).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# Usuario no-root y directorio de datos persistente (BD + logs).
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /data \
    && chown -R bot:bot /data /app
USER bot

VOLUME ["/data"]

CMD ["python", "-m", "p2p_arb_bot.main"]
