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
     precios, métodos y los **dos enlaces a las publicaciones** (compra y venta),
     y la guarda en SQLite.
   - Si no: imprime una línea de estado discreta (heartbeat) con el mejor
     spread actual.

Para correr **sin interacción** (cron/servidor), pon `NO_INPUT=true` en `.env`
y define el resto de variables.

`Ctrl+C` cierra la conexión HTTP y la base de datos de forma limpia.

## Dashboard web

Un dashboard (FastAPI + HTMX) permite **supervisar, ejecutar y configurar** el bot
desde el navegador. La **supervisión es pública** (no requiere login); **arrancar /
parar el bot y editar la configuración exigen contraseña**.

```bash
make dashboard      # local: http://localhost:8000
```

Configura antes en `.env`:

- `DASHBOARD_PASSWORD` — contraseña para las acciones protegidas (sin ella, el
  dashboard queda en modo solo-lectura).
- `DASHBOARD_SECRET` — secreto para firmar la cookie de sesión
  (`python -c "import secrets; print(secrets.token_hex(32))"`).
- `DASHBOARD_HOST` / `DASHBOARD_PORT` — interfaz y puerto (por defecto `0.0.0.0:8000`).

Qué ofrece:

- **Supervisar** (público): estado del bot (corriendo/detenido + uptime), mejor
  spread neto actual, oportunidades de las últimas 24 h, profit estimado, tabla de
  oportunidades recientes y las últimas líneas del log. Todo se auto-refresca.
- **Ejecutar** (login): botones de arrancar / parar / reiniciar. El dashboard
  gestiona el bot como subproceso.
- **Configurar** (login): edita los parámetros clave (umbral %, monto máx USDT,
  métodos de pago, intervalo) y los aplica reiniciando el bot. Escribe en `.env`
  preservando el resto de claves.

El estado en vivo lo alimenta un `StatusNotifier` que el bot añade a su lista de
notifiers: vuelca el último heartbeat/oportunidad a `STATUS_PATH` (`status.json`),
así el dashboard no necesita pegarle a Binance por su cuenta.

En Docker el dashboard es el **contenedor principal** y arranca el bot como
subproceso (ver más abajo).

## Despliegue con Docker

Requiere Docker con el plugin Compose. El contenedor corre el **dashboard web**
(`http://localhost:8000`), que arranca/para el bot como subproceso en modo **no
interactivo** (`NO_INPUT=true`). La configuración se toma de variables de entorno /
`.env`. La base de datos, los logs y el estado se guardan en un volumen
(`arb-data`, montado en `/data`) que persiste entre reinicios; el `.env` se
bind-montea para que la edición desde el dashboard persista.

```bash
make env            # crea .env desde .env.example (ajústalo a tu gusto)
make docker-up      # construye y levanta el dashboard en segundo plano
make docker-logs    # sigue la salida
make docker-down    # detiene y elimina el contenedor
```

Luego abre `http://localhost:8000` y arranca el bot desde el dashboard (necesitas
`DASHBOARD_PASSWORD` en `.env`).

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
make docker-run     # corre el bot interactivo dentro del contenedor del dashboard
```

El contenedor necesita **salida a Internet** hacia `p2p.binance.com`. Si tu red
de Docker la bloquea o Binance limita la IP, define `PROXY` en `.env` (se respeta
dentro del contenedor) o ajusta la red del host. Errores `curl: (28) timed out`
en los logs indican precisamente falta de egress.

### Makefile

`make` (o `make help`) lista todos los objetivos: `install`, `test`, `run`,
`dashboard`, `clean`, `env`, y los `docker-*` (`docker-build`, `docker-up`,
`docker-down`, `docker-logs`, `docker-run`, `docker-shell`).

### Terminología

- `BUY`  → anuncios donde **tú compras** USDT (precio que pagas). El mejor es el
  **más bajo**.
- `SELL` → anuncios donde **tú vendes** USDT (precio que recibes). El mejor es el
  **más alto**.

Hay oportunidad cuando `best_sell > best_buy` y `net_pct ≥ umbral`, con
`net_pct = spread_pct − fee_buffer_pct`.

### Interpretar el heartbeat (`sin datos / sin pares elegibles`)

Cuando **no** hay oportunidad, el bot imprime una línea de estado (heartbeat) en
lugar de un panel. Puede decir dos cosas:

- **`mejor spread neto X%`** — sí encontró al menos una pareja compra/venta
  elegible, pero el mejor spread quedó por debajo de tu umbral. Es lo normal la
  mayor parte del tiempo.
- **`sin datos / sin pares elegibles`** — en ese ciclo **no encontró ni una sola
  pareja compra-venta con la que calcular un spread**. No es un error: el bot está
  avisando honestamente de que no había nada que comparar.

Para poder comparar, el bot necesita **al menos un anuncio de compra Y uno de
venta que sobrevivan a estos 4 filtros** (`domain/arbitrage.py`, función
`_eligible`). Si **cualquiera de los dos lados** queda vacío, no hay par →
`sin datos / sin pares elegibles`:

1. **Método de pago** — que el anuncio use uno de tus `PAY_METHODS`
   (vacío = se aceptan todos).
2. **Acepta tu monto** (`accepts_amount`) — que tu operación quepa entre el
   mín/máx del anuncio. El monto fiat se calcula como `MAX_USDT × precio`
   (p. ej. `100 USDT × 825 ≈ 82.500 VES`), así que un `MAX_USDT` bajo puede no
   encajar en anuncios con mínimos altos, y uno alto puede pasarse del máximo.
3. **Inventario** (`has_inventory`) — que al anunciante le queden ≥ `MAX_USDT`
   disponibles (`surplusAmount`).
4. **Outliers** — que el precio no se desvíe de la mediana más de
   `OUTLIER_MAX_DEV_PCT` (descarta precios "cebo"). `0` desactiva este filtro.

Si ves ese mensaje de forma persistente y **sin que se registren oportunidades**
(la tabla `opportunities` sigue vacía), casi siempre es por **configuración
demasiado restrictiva**, no por un fallo de red: revisa `MAX_USDT` (prueba un
valor que encaje con los límites típicos del par), reduce la lista de
`PAY_METHODS` o déjala vacía, y afloja/ajusta `OUTLIER_MAX_DEV_PCT`. Que el bot
traiga anuncios pero no forme pares confirma que el egress a Binance funciona; el
cuello de botella son los filtros de elegibilidad.

## Base de datos

Tabla `opportunities` (SQLite, ruta en `DB_PATH`) con `detected_at`, par,
métodos de cada pierna, precios, `spread_pct`, `net_pct`, `max_usdt`, los
`advNo` de cada anuncio, anunciantes, los enlaces a cada publicación y `est_profit_usdt`
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
