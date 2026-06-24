# P2P Arb Bot — Monitor de arbitraje Binance P2P (USDT/VES)

Bot en Python que **monitorea** el mercado P2P de Binance para un par (por
defecto USDT/VES), detecta oportunidades de arbitraje (spread entre el mejor
precio de compra y el mejor de venta) según los métodos de pago y el monto en
USDT que indiques, **avisa por consola** cuando la ganancia neta supera un
umbral, y guarda cada oportunidad en **SQLite** con el % de ganancia y los
enlaces a los anuncios.

> ⚠️ **El bot SOLO monitorea y avisa.** No ejecuta operaciones, no publica
> anuncios y **no** usa API keys de trading de Binance. Usa únicamente el
> endpoint público de búsqueda de anuncios.

## Arquitectura

Arquitectura limpia con dependencias apuntando hacia el dominio:

```
src/p2p_arb_bot/
├── domain/          # Modelos puros + lógica de arbitraje (sin I/O, testeable)
│   ├── models.py
│   └── arbitrage.py
├── application/     # Casos de uso + puertos (Protocols)
│   ├── ports.py     # MarketDataSource, OpportunityRepository, Notifier
│   └── monitor.py   # MonitorService: buscar → analizar → persistir → notificar
├── infrastructure/  # Adaptadores concretos
│   ├── binance_p2p.py     # MarketDataSource con curl_cffi (async, impersonation TLS)
│   ├── discovery.py       # Descubre métodos de pago disponibles
│   ├── sqlite_repo.py     # OpportunityRepository (SQLite)
│   └── console_notifier.py# Notifier (rich)
├── config.py        # Configuración tipada + carga .env
└── main.py          # Composition root: wiring, CLI, loop, cierre limpio
```

La inversión de dependencias (puertos con `typing.Protocol` + inyección por
constructor) permite cambiar piezas sin tocar el núcleo: otra fuente de datos
(Bybit/OKX), otro repositorio (Postgres/CSV) u otro notificador (Telegram,
webhook). Notificadores y repositorios se pasan como **listas**, así puedes
combinar varios a la vez. La configuración admite una lista de *watch targets*,
de modo que escalar a multi-par / multi-fiat no requiere cambios estructurales.

## Instalación

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e .          # registra el paquete (src/) y el comando p2p-arb-bot
```

Requiere Python 3.11+. El `pip install -e .` es lo que permite ejecutar
`python -m p2p_arb_bot.main`; sin él, el paquete vive en `src/` y Python no lo
encuentra. (Alternativa sin instalar: `PYTHONPATH=src python -m p2p_arb_bot.main`.)

## Uso

```bash
cp .env.example .env   # opcional: ajusta valores por defecto
python -m p2p_arb_bot.main
```

Al arrancar, el bot:

1. Descubre los métodos de pago disponibles para el par y los muestra numerados.
2. Te pide elegir uno o varios (Enter = todos), el monto máximo en USDT, el
   umbral de ganancia %, el buffer de fees %, el intervalo de polling y si
   filtrar solo comerciantes verificados. Cada prompt usa el valor del `.env`
   como predeterminado (Enter para aceptarlo).
3. Entra en bucle: consulta ambos lados (BUY/SELL) en paralelo, calcula el
   mejor spread (incluyendo **pares cruzados** entre métodos), y:
   - Si hay oportunidad ≥ umbral: imprime un panel destacado con % neto/bruto,
     precios, métodos, anunciantes y los **dos enlaces**, y la guarda en SQLite.
   - Si no: imprime una línea de estado discreta (heartbeat) con el mejor
     spread actual.

Para correr **sin interacción** (cron/servidor), pon `NO_INPUT=true` en `.env`
y define el resto de variables.

`Ctrl+C` cierra la conexión HTTP y la base de datos de forma limpia.

## Despliegue con Docker

Requiere Docker con el plugin Compose. La imagen corre el bot en modo **no
interactivo** (`NO_INPUT=true`), tomando la configuración de variables de
entorno / `.env`. La base de datos y los logs se guardan en un volumen
(`arb-data`, montado en `/data`) que persiste entre reinicios.

```bash
make env            # crea .env desde .env.example (ajústalo a tu gusto)
make docker-up      # construye y levanta el bot en segundo plano
make docker-logs    # sigue la salida
make docker-down    # detiene y elimina el contenedor
```

O directamente con Compose:

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f
```

En modo no interactivo no hay menú de métodos: define `PAY_METHODS` en `.env`
(vacío = vigilar todos) junto con `MAX_USDT`, `THRESHOLD_PCT`, etc. Para
**descubrir métodos** o configurar a mano dentro del contenedor, lánzalo
interactivo:

```bash
make docker-run     # = docker compose run --rm -e NO_INPUT=false bot
```

El contenedor necesita **salida a Internet** hacia `p2p.binance.com`. Si tu red
de Docker la bloquea o Binance limita la IP, define `PROXY` en `.env` (se respeta
dentro del contenedor) o ajusta la red del host. Errores `curl: (28) timed out`
en los logs indican precisamente falta de egress.

### Makefile

`make` (o `make help`) lista todos los objetivos: `install`, `test`, `run`,
`clean`, `env`, y los `docker-*` (`docker-build`, `docker-up`, `docker-down`,
`docker-logs`, `docker-run`, `docker-shell`).

### Terminología

- `BUY`  → anuncios donde **tú compras** USDT (precio que pagas). El mejor es el
  **más bajo**.
- `SELL` → anuncios donde **tú vendes** USDT (precio que recibes). El mejor es el
  **más alto**.

Hay oportunidad cuando `best_sell > best_buy` y `net_pct ≥ umbral`, con
`net_pct = spread_pct − fee_buffer_pct`.

## Base de datos

Tabla `opportunities` (SQLite, ruta en `DB_PATH`) con `detected_at`, par,
métodos de cada pierna, precios, `spread_pct`, `net_pct`, `max_usdt`, los
`advNo` de cada anuncio, anunciantes, los enlaces y `est_profit_usdt`
(ganancia neta estimada en USDT = `max_usdt * net_pct / 100`). Índice por
`detected_at`. Las BDs creadas con un esquema anterior se migran solas al
arrancar (se añade la columna nueva si falta).
Se evitan duplicados idénticos (mismos `advNo` + precios) dentro de una ventana
de 60 s.

Consulta rápida:

```bash
sqlite3 opportunities.db "SELECT detected_at, net_pct, buy_price, sell_price FROM opportunities ORDER BY detected_at DESC LIMIT 10;"
```

## Configuración (.env)

Todas las variables están documentadas en [`.env.example`](.env.example). Las
más relevantes: `ASSET`, `FIAT`, `PAY_METHODS`, `MAX_USDT`, `THRESHOLD_PCT`,
`FEE_BUFFER_PCT`, `MERCHANT_CHECK`, `POLL_INTERVAL_S`, `DB_PATH`, `IMPERSONATE`,
`PROXY`, `BEEP`, `NO_INPUT`.

### Anti-bloqueo

El endpoint puede hacer fingerprinting TLS/JA3, por lo que `requests`/`httpx`
pueden bloquearse. Se usa `curl_cffi` con `impersonate="chrome"`, que replica el
handshake de Chrome sin necesidad de navegador. Hay backoff exponencial con
jitter ante 403/429/timeouts, validación de `success:true` (un 200 no basta) y
polling con jitter. Se soporta proxy opcional vía `PROXY`/`HTTPS_PROXY` (sin
credenciales en el código).

## Tests

```bash
pytest
```

Los tests cubren la capa de dominio (spread, filtrado por límites, pares
cruzados) y el caso de uso (persistencia, supresión de duplicados, heartbeats,
múltiples adaptadores) usando fakes, **sin red ni disco**.
