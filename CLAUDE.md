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
```

Setup inicial: `python -m venv venv && source venv/bin/activate && make install`.
Requiere Python 3.11+. Sin `pip install -e .`, usa `PYTHONPATH=src`.

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

- "BUY" = anuncios donde **el usuario compra** USDT → mejor = precio **más bajo**.
- "SELL" = anuncios donde **el usuario vende** USDT → mejor = precio **más alto**.
- Hay oportunidad si `best_sell > best_buy` y `net_pct ≥ threshold_pct`, con
  `net_pct = spread_pct − fee_buffer_pct`.
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

## Configuración

Todo vía `.env` (ver `.env.example`, documentado). `Defaults.from_env()` los carga
como predeterminados; en modo interactivo cada prompt los ofrece como default.
`NO_INPUT=true` salta los prompts (cron/Docker). Claves: `ASSET`, `FIAT`,
`PAY_METHODS`, `MAX_USDT`, `THRESHOLD_PCT`, `FEE_BUFFER_PCT`, `OUTLIER_MAX_DEV_PCT`,
`MERCHANT_CHECK`, `POLL_INTERVAL_S`, `DB_PATH`, `LOG_PATH`, `STATUS_PATH`,
`SCREENSHOTS_DIR`, `SCREENSHOTS`, `IMPERSONATE`, `PROXY`, `BEEP`, `NO_INPUT`,
`DASHBOARD_*`, `ENV_PATH`, `BOT_PIDFILE`.

## Base de datos

Tabla `opportunities` en SQLite (`DB_PATH`). Migración automática al arrancar
(añade columnas faltantes). Se suprimen duplicados idénticos (mismos `advNo` +
precios) dentro de una ventana de 60 s (`dedup_key` / `recent_duplicate`).

## Tests

`pytest`. Cubren dominio (spread, límites, pares cruzados, outliers), caso de uso
(persistencia, supresión de duplicados, heartbeats, múltiples adaptadores) y web,
usando **fakes — sin red ni disco**. Al añadir lógica al dominio o a `MonitorService`,
escribe el test con un fake que cumpla el Protocol correspondiente; no hagas red real.

## Docker

El contenedor principal corre el **dashboard** (`CMD python -m p2p_arb_bot.web`), que
arranca el bot como subproceso en modo `NO_INPUT=true`. DB/logs/estado van al volumen
`arb-data` montado en `/data`; el `.env` se bind-montea para que la edición persista.
El contenedor necesita egress a `p2p.binance.com` (errores `curl: (28) timed out` =
falta de salida a Internet; usa `PROXY`). Para descubrir métodos de pago hace falta
modo interactivo: `make docker-run`.
