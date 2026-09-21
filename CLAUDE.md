# CLAUDE.md

Guía para trabajar en este repositorio. El proyecto y los comentarios están en
**español**: mantén ese idioma en código, comentarios, docstrings y mensajes de commit.

## Qué es

`p2p-arb-bot`: monitor (**solo aviso, no opera**) de arbitraje en el mercado
Binance P2P (por defecto USDT/VES). Consulta el endpoint público de búsqueda de
anuncios, detecta spreads entre el mejor lado de compra y de venta, avisa cuando
la ganancia neta supera un umbral y guarda cada oportunidad en SQLite. Incluye un
dashboard web (FastAPI + HTMX) para supervisar, arrancar/parar y configurar el bot.

No usa API keys de trading; solo el endpoint público
`https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search`.

## Comandos

```bash
make install      # instala deps + paquete editable en venv/ (créalo antes: make venv)
make test         # pytest (rápido: sin red ni disco, usa fakes)
make run          # bot interactivo local (python -m p2p_arb_bot.main)
make dashboard    # dashboard web local en http://localhost:8000
make docker-up    # despliega el dashboard en Docker (bot como subproceso)
make help         # lista todos los objetivos
pytest tests/test_arbitrage.py   # un solo archivo de tests
pytest -k "outlier"              # un solo test por nombre
```

Setup inicial: `python -m venv venv && source venv/bin/activate && make install`.
Requiere Python 3.11+. Sin `pip install -e .`, usa `PYTHONPATH=src`.

**No hay linter ni formateador configurados** (`make lint` está en `.PHONY` pero no
existe el objetivo: fallaría). No añadas ruff/black/mypy sin pedirlo antes.

`pip install -e .` registra además dos comandos: `p2p-arb-bot` (el bot) y
`p2p-arb-dashboard` (el dashboard), equivalentes a los `python -m ...`.

## Arquitectura (clean architecture, dependencias hacia el dominio)

```
src/p2p_arb_bot/
├── domain/          # Modelos puros + lógica de arbitraje. SIN I/O. Testeable aislado.
│   ├── models.py    # Ad, WatchTarget, Opportunity (dataclasses frozen)
│   └── arbitrage.py # best_buy/best_sell, drop_outliers, find_best_opportunity
├── application/     # Casos de uso + puertos (typing.Protocol)
│   ├── ports.py     # MarketDataSource, OpportunityRepository, Notifier
│   └── monitor.py   # MonitorService: buscar→analizar→persistir→notificar
├── infrastructure/  # Adaptadores concretos (implementan los puertos)
│   ├── binance_p2p.py     # MarketDataSource con curl_cffi (async, TLS impersonation)
│   ├── discovery.py       # Descubre métodos de pago disponibles
│   ├── sqlite_repo.py     # OpportunityRepository (SQLite)
│   ├── console_notifier.py# Notifier (rich)
│   ├── status_notifier.py # Notifier que vuelca status.json para el dashboard
│   └── screenshot_notifier.py # Notifier que guarda un PNG "ficha" por oportunidad (Pillow)
├── config.py        # Defaults.from_env() + AppConfig (tipado, validado)
├── main.py          # Composition root: ÚNICO módulo que conoce las clases concretas
└── web/             # Dashboard FastAPI + HTMX (proceso aparte del bot)
    ├── app.py       # create_app(): rutas, sesiones, plantillas
    ├── bot_manager.py # arranca/para el bot como SUBPROCESO vía pidfile
    ├── db_reader.py # lectura solo-lectura de DB/status.json/log
    ├── trade_store.py # registro de operaciones REALES del usuario (DB propia)
    ├── env_store.py # lee/escribe el .env desde el form de config
    └── auth.py      # login por contraseña + cookie de sesión firmada
```

**Regla de oro de la inversión de dependencias:** el dominio y la aplicación
dependen solo de los puertos (`typing.Protocol`), nunca de implementaciones. Las
clases concretas se instancian e inyectan **únicamente en `main.py`**. Si añades
una nueva fuente de datos, repositorio o notificador, impleméntala en
`infrastructure/`, cumple el Protocol y conéctala en el composition root — no
toques `domain/` ni `application/`.

Notificadores y repositorios se pasan como **listas** a `MonitorService`, así se
combinan varios (consola + SQLite + status.json). La config soporta una **lista de
`WatchTarget`**, de modo que multi-par/multi-fiat no requiere cambios estructurales.

**Multi-cripto**: `ASSETS` (CSV) genera un `WatchTarget` por moneda en
`main.build_config()`, todas contra el mismo `FIAT`. El catálogo de monedas
ofrecidas está fijo en `SUPPORTED_ASSETS` (`config.py`) — es catálogo de
configuración, no dominio: el motor vigila cualquier asset que le llegue en un
target. `POLL_INTERVAL_S` es por **barrido completo**, no por moneda.

## Convenciones importantes

- **`Decimal` para todo precio/monto**, nunca `float`. El parseo de la API y los
  cálculos usan `Decimal`; los floats solo aparecen al serializar a JSON.
- **Dataclasses `frozen=True, slots=True`** para los modelos del dominio.
- **Async**: el caso de uso y la fuente de datos son async. `MonitorService`
  consulta BUY y SELL en paralelo (`asyncio.gather`) y limita concurrencia con un
  semáforo. Los prompts interactivos usan `asyncio.to_thread(input, ...)`.
- **Un fallo en un ciclo no mata el bucle** (`run_forever` captura y loguea).
- **Logging**: detalle al archivo (`LOG_PATH`), solo WARNING+ a consola.

### Lógica de arbitraje (`domain/arbitrage.py`)

- "BUY" = anuncios donde **el usuario compra** la cripto → mejor = precio **más bajo**.
- "SELL" = anuncios donde **el usuario vende** la cripto → mejor = precio **más alto**.
- Hay oportunidad si `best_sell > best_buy` y `net_pct ≥ threshold_pct`, con
  `net_pct = spread_pct − fee_buffer_pct`.
- **El fondo (`WatchTarget.max_fiat`, de `MAX_FIAT`) va en moneda FIAT**, no en
  unidades de la cripto: es lo único comparable entre monedas (100 tiene sentido
  en USDT y ninguno en BTC). `accepts_amount` compara ese fondo directamente
  contra los límites del anuncio (que ya vienen en fiat) y `has_inventory` lo
  divide por el precio para contrastarlo con `surplusAmount`.
  `Opportunity.max_usdt` guarda las **unidades** que compra el fondo a ese precio
  (`max_fiat / buy_price`); conserva el nombre por la columna de SQLite.
- Se evalúan **pares cruzados** entre métodos de pago (comprar con método A,
  vender con B).
- `drop_outliers` descarta precios "cebo" que se desvían más de
  `OUTLIER_MAX_DEV_PCT` de la mediana (necesita ≥3 anuncios; 0 desactiva).

### Anti-bloqueo (`infrastructure/binance_p2p.py`)

El endpoint hace fingerprinting TLS/JA3 → `requests`/`httpx` se bloquean. Se usa
**`curl_cffi` con `impersonate="chrome"`**. NO fijar `User-Agent` manualmente
(rompería la coherencia del fingerprint). Hay backoff exponencial con jitter ante
403/429/503/timeout, y un 200 **no basta**: se valida `success:true` en el cuerpo.
Una **sesión por petición** (no compartida) para evitar deadlocks de libcurl con
transferencias concurrentes. Proxy opcional vía `PROXY`/`HTTPS_PROXY`, nunca
credenciales hardcodeadas.

## Dashboard y bot: dos procesos desacoplados

El dashboard **no importa** `MonitorService`. Controla el bot como **subproceso**
(`BotManager`, vía pidfile en disco que sobrevive a reinicios del dashboard) y lee
su salida en solo-lectura (DB, `status.json`, log). El bot publica su estado en vivo
escribiendo `status.json` (vía `StatusNotifier`) con rename atómico. El bot lanzado
por el dashboard corre siempre con `NO_INPUT=true`.

- **Supervisión pública** (sin login); **arrancar/parar/configurar exigen login**
  (`DASHBOARD_PASSWORD` + cookie firmada con `DASHBOARD_SECRET`).
- Editar la config desde el dashboard reescribe el `.env` (`env_store`) preservando
  el resto de claves, y reinicia el bot si está corriendo.
- **Supervisión pública, registro de operaciones privado.** Las oportunidades y el
  log son visibles sin login; todo lo de `trades` (incluida la LECTURA) exige sesión,
  porque lleva precios reales, contrapartes y notas. Ojo: si el dashboard se expone a
  un dominio público, `/partials/log` y `/partials/status` quedan abiertos a Internet.
- HTMX está **vendorizado** en `web/static/htmx.min.js`, no viene de un CDN.
- **JS propio: solo `web/static/notify.js`** (avisos del navegador). El resto del
  front es HTMX más algún `hx-on` inline. Las notificaciones exigen **contexto
  seguro** (HTTPS o localhost): por IP de LAN el navegador bloquea la API y el botón
  se deshabilita solo. El "ya visto" vive en `localStorage`, no en el servidor.
- Los assets no-Python (`web/templates/**`, `web/static/*`,
  `infrastructure/fonts/*.ttf`) se enumeran a mano en `[tool.setuptools.package-data]`
  de `pyproject.toml`. Si creas otra subcarpeta de plantillas o añades una fuente,
  agrégala ahí o no se instalará con el paquete (funcionará en editable y fallará
  en Docker).

## Configuración

Todo vía `.env` (ver `.env.example`, documentado). `Defaults.from_env()` los carga
como predeterminados; en modo interactivo cada prompt los ofrece como default.
`NO_INPUT=true` salta los prompts (cron/Docker). Claves: `ASSETS`, `FIAT`,
`PAY_METHODS`, `MAX_FIAT`, `THRESHOLD_PCT`, `FEE_BUFFER_PCT`, `OUTLIER_MAX_DEV_PCT`,
`MERCHANT_CHECK`, `POLL_INTERVAL_S`, `DB_PATH`, `LOG_PATH`, `STATUS_PATH`,
`SCREENSHOTS_DIR`, `SCREENSHOTS`, `IMPERSONATE`, `PROXY`, `BEEP`, `NO_INPUT`,
`ROWS`, `DASHBOARD_*`, `ENV_PATH`, `BOT_PIDFILE`, `TRADES_DB_PATH`.

`TRADES_DB_PATH` es solo del dashboard: se lee en `web/app.py` (`_paths()`), **no**
en `Defaults.from_env()`, porque el bot no conoce ese fichero.

`ASSET` (singular) sigue leyéndose como **alias legado** de `ASSETS` para no romper
los `.env` ya escritos. `MAX_USDT`, en cambio, **ya no se lee**: lo sustituye
`MAX_FIAT` y un `.env` viejo que lo conserve lo verá ignorado en silencio.

Al añadir una clave nueva: documéntala en `.env.example` **y** léela en
`Defaults.from_env()`. Ojo, `ROWS` y `BOT_PIDFILE` sí se leen (`config.py`,
`web/app.py`) pero faltan en `.env.example`; no tomes ese archivo como la lista
completa.

## Base de datos

Tabla `opportunities` en SQLite (`DB_PATH`). Migración automática al arrancar
(añade columnas faltantes). Se suprimen duplicados idénticos (mismos `advNo` +
precios) dentro de una ventana de 60 s (`dedup_key` / `recent_duplicate`).

**Son DOS ficheros SQLite, y la separación es deliberada.** `DB_PATH` la escribe el
bot; `TRADES_DB_PATH` (tabla `trades`, `web/trade_store.py`) la escribe el dashboard
con las operaciones que el usuario ejecutó a mano. No se pueden fusionar: SQLite
bloquea **el fichero entero, no la tabla**, y el bot no activa WAL, así que dos
procesos escribiendo el mismo fichero se pelean por el lock. Con ficheros separados
cada proceso escribe el suyo (verificado: 2000 escrituras del bot en paralelo, cero
`database is locked`).

Consecuencia a tener presente: **leer `DB_PATH` bajo escritura intensa del bot sí
puede fallar** con `database is locked` (un lector bloquea al escritor y viceversa
en modo rollback journal). `db_reader` lo traga y degrada a lista vacía. Por eso el
formulario de registro **lleva el snapshot de lo estimado en campos ocultos** en vez
de releer la DB en el POST: una lectura bloqueada guardaría la operación sin
estimado y destruiría en silencio la comparación estimado-vs-real.

Cada fila de `trades` **copia** lo que estimó el bot (`est_net_pct`, `detected_at`…)
en vez de referenciar `opportunity_id`, porque el botón Limpiar hace
`DELETE FROM opportunities` y dejaría el registro sin base de comparación.

## Tests

`pytest`. Cubren dominio (spread, límites, pares cruzados, outliers), caso de uso
(persistencia, supresión de duplicados, heartbeats, múltiples adaptadores) y web.
Al añadir lógica al dominio o a `MonitorService`, escribe el test con un fake que
cumpla el Protocol correspondiente; **no hagas red real**.

Hay tres estilos y cada uno cubre cosas distintas — usa el que toque:

- `test_arbitrage.py`, `test_monitor.py`: **fakes, sin red ni disco**. Dominio y caso
  de uso.
- `test_web.py`, `test_trade_store.py`: funciones puras de soporte con **SQLite real
  sobre `tmp_path`**.
- `test_web_endpoints.py`: la app montada con **`TestClient`** (requiere `httpx2`;
  Starlette 1.3 lo prefiere sobre el `httpx` clásico, que emite deprecation). Cubre
  lo que los otros dos no pueden ver: plantillas que revientan, rutas sin proteger y
  contexto que no llega al parcial. **Si tocas `web/app.py` o una plantilla, el test
  va aquí** — un fallo de render solo aparece pidiendo la ruta.
  Ojo: `create_app()` llama a `load_dotenv`, así que **hay que fijar `ENV_PATH`** a un
  archivo vacío o los tests cargarían el `.env` real del repo. No toques `/bot/start`
  desde un test: lanza un subproceso de verdad.

## Docker

El contenedor principal corre el **dashboard** (`CMD python -m p2p_arb_bot.web`), que
arranca el bot como subproceso en modo `NO_INPUT=true`. DB/logs/estado van al volumen
`arb-data` montado en `/data`; el `.env` se bind-montea para que la edición persista.
El contenedor necesita egress a `p2p.binance.com` (errores `curl: (28) timed out` =
falta de salida a Internet; usa `PROXY`). Para descubrir métodos de pago hace falta
modo interactivo: `make docker-run`.
