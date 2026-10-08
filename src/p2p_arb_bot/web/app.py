"""App FastAPI del dashboard: wiring de rutas, sesiones, plantillas y estado.

Punto de entrada del proceso web. No importa ``MonitorService``: controla el bot
como subproceso (``BotManager``) y lee su salida (DB, status.json, log) en modo
solo-lectura. Así el dashboard y el bot quedan desacoplados.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db_reader, env_store, trade_store, market_reader, market_view
from .bot_manager import BotManager
from .fund_store import FundStore
from ..config import Defaults, validate_storage_paths
from ..infrastructure.control_store import ControlStore
from ..infrastructure.market_codec import target_dict, read_route, read_target

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))


def _paths() -> dict:
    """Rutas de datos (mismas que usa el bot vía entorno)."""
    return {
        "db_path": os.getenv("DB_PATH", "opportunities.db") or "opportunities.db",
        "log_path": os.getenv("LOG_PATH", "p2p_arb_bot.log") or "p2p_arb_bot.log",
        "status_path": os.getenv("STATUS_PATH", "status.json") or "status.json",
        "env_path": os.getenv("ENV_PATH", ".env") or ".env",
        # Fichero propio del dashboard (el bot no lo conoce): ver trade_store.
        "trades_db_path": os.getenv("TRADES_DB_PATH", "trades.db") or "trades.db",
    }


def _humanize_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _fmt_miles(value: object) -> str:
    """Formatea un número con separador de miles con punto (convención VES): 820000 → '820.000'."""
    try:
        return f"{round(float(value)):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"
    except Exception:  # p. ej. un Undefined de Jinja: degradar sin romper la página
        return "—"


def _fmt_unidades(value: object) -> str:
    """Formatea unidades de una cripto sin decimales de más ni de menos.

    ``%.2f`` sirve para USDT pero convierte 0.00012 BTC en un inútil "0.00". Se
    usan 8 decimales (la precisión de BTC) y se recortan los ceros a la derecha.
    """
    try:
        texto = f"{float(value):.8f}".rstrip("0").rstrip(".")
        return texto or "0"
    except (TypeError, ValueError):
        return "—"
    except Exception:  # Undefined de Jinja: degradar sin romper la página
        return "—"


def create_app() -> FastAPI:
    # Carga .env para que DASHBOARD_PASSWORD/SECRET y las rutas estén disponibles
    # vía os.getenv, igual que hace el bot en Defaults.from_env(). Sin esto, el
    # login diría que la contraseña no está configurada aunque esté en el .env.
    load_dotenv(os.getenv("ENV_PATH") or None)

    app = FastAPI(title="P2P Arb Dashboard")

    _TEMPLATES.env.filters["miles"] = _fmt_miles
    _TEMPLATES.env.filters["unidades"] = _fmt_unidades

    secret = os.getenv("DASHBOARD_SECRET") or secrets.token_hex(32)
    app.add_middleware(SessionMiddleware, secret_key=secret)

    @app.middleware("http")
    async def prevent_private_cache(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        # Una misma URL pública puede incluir fondos cuando hay sesión.
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Vary"] = "Cookie"
        return response

    static_dir = _HERE / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    paths = _paths()
    validate_storage_paths(paths["db_path"], Defaults.from_env().control_db_path, paths["trades_db_path"])
    bot = BotManager(
        pidfile=os.getenv("BOT_PIDFILE")
        or os.path.join(os.path.dirname(os.path.abspath(paths["db_path"])), "bot.pid"),
        log_path=paths["log_path"],
    )

    def _funds() -> FundStore:
        return FundStore(_paths()["trades_db_path"])

    def _queue() -> ControlStore:
        cfg = Defaults.from_env()
        return ControlStore(cfg.control_db_path, ttl_s=cfg.revalidation_ttl_s,
                            cooldown_s=cfg.revalidation_cooldown_s,
                            retention_days=cfg.market_retention_days,
                            max_pending=cfg.revalidation_max_pending)

    def _market_context(request: Request, message: str | None = None, error: str | None = None) -> dict:
        cfg = Defaults.from_env()
        books = market_reader.books(_paths()["db_path"], cfg.market_freshness_s)
        ctx = {"request": request, "authed": auth.is_authed(request), "message": message,
               "error": error, "episodes": market_reader.episodes(_paths()["db_path"], cfg.market_freshness_s),
               "recommendations": [], "revalidations": [], "funds_revision": 0, "books": books,
               "revalidation_metrics": {}}
        if ctx["authed"]:
            accounts, revision = _funds().snapshot()
            queue = _queue()
            requests = queue.recent()
            ctx.update({"funds_revision": revision, "revalidations": requests,
                        "revalidation_metrics": queue.metrics(),
                        "recommendations": market_view.recommendations(
                            books,
                            accounts, revision, requests, cfg.market_freshness_s, cfg.market_max_routes)})
        return ctx

    def _fund_context(request: Request, message: str | None = None, error: str | None = None) -> dict:
        store = _funds()
        accounts, revision = store.snapshot()
        return {"request": request, "authed": True, "accounts": accounts, "funds_revision": revision,
                "reservations": store.reservations(), "changes": store.changes(),
                "fiat": Defaults.from_env().fiat, "message": message, "error": error}

    # --- helpers de contexto --------------------------------------------

    def _dashboard_context(request: Request) -> dict:
        p = _paths()
        return {
            **_market_context(request),
            "request": request,
            "authed": auth.is_authed(request),
            "bot": _bot_view(),
            "cfg": env_store.read_config(),
            "stats": db_reader.stats_24h(p["db_path"]),
            "status": db_reader.read_status(p["status_path"]),
            "opportunities": db_reader.recent_opportunities(p["db_path"], limit=50),
        }

    def _bot_view() -> dict:
        st = bot.status()
        st["uptime_h"] = _humanize_uptime(st.get("uptime_s"))
        return st

    # --- supervisión (público) ------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(request, "dashboard.html", _dashboard_context(request))

    @app.get("/partials/market", response_class=HTMLResponse)
    def partial_market(request: Request) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(request, "partials/market.html", _market_context(request))

    @app.get("/funds", response_class=HTMLResponse)
    def funds_page(request: Request, _: None = Depends(auth.require_login)) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(request, "funds.html", _fund_context(request))

    @app.post("/funds/accounts", response_class=HTMLResponse)
    def fund_save(request: Request, name: str = Form(...), fiat: str = Form(...),
                  balance: str = Form(...), methods: str = Form(...),
                  account_id: int = Form(0), version: int = Form(0), can_receive: bool = Form(False),
                  _: None = Depends(auth.require_login)) -> HTMLResponse:
        try:
            _funds().save(name=name, fiat=fiat, balance=balance, methods=methods,
                          account_id=account_id, expected_version=version, can_receive=can_receive)
        except ValueError as exc:
            return _TEMPLATES.TemplateResponse(request, "funds.html", _fund_context(request, error=str(exc)), status_code=400)
        return RedirectResponse("/funds", status_code=303)

    @app.post("/funds/accounts/{account_id}/archive", response_class=HTMLResponse)
    def fund_archive(request: Request, account_id: int, version: int = Form(...),
                     _: None = Depends(auth.require_login)) -> HTMLResponse:
        try:
            _funds().archive(account_id, version)
        except ValueError as exc:
            return _TEMPLATES.TemplateResponse(request, "funds.html", _fund_context(request, error=str(exc)), status_code=400)
        return RedirectResponse("/funds", status_code=303)

    @app.post("/funds/reservations", response_class=HTMLResponse)
    def fund_reserve(request: Request, account_id: int = Form(...), amount: str = Form(...),
                     version: int = Form(...), note: str = Form(""),
                     _: None = Depends(auth.require_login)) -> HTMLResponse:
        try:
            _funds().reserve(account_id, amount, expected_version=version, note=note)
        except ValueError as exc:
            return _TEMPLATES.TemplateResponse(request, "funds.html", _fund_context(request, error=str(exc)), status_code=400)
        return RedirectResponse("/funds", status_code=303)

    @app.post("/funds/reservations/{reservation_id}/release", response_class=HTMLResponse)
    def fund_release(request: Request, reservation_id: int,
                     _: None = Depends(auth.require_login)) -> HTMLResponse:
        try:
            _funds().release(reservation_id)
        except ValueError as exc:
            return _TEMPLATES.TemplateResponse(request, "funds.html", _fund_context(request, error=str(exc)), status_code=400)
        return RedirectResponse("/funds", status_code=303)

    @app.post("/market/revalidate", response_class=HTMLResponse)
    def revalidate(request: Request, route_key: str = Form(...), account_id: int = Form(0),
                   _: None = Depends(auth.require_login)) -> HTMLResponse:
        ctx = _market_context(request)
        try:
            if not bot.is_running():
                raise ValueError("El bot está detenido. Arráncalo para comprobar el mercado.")
            row = next((r for r in ctx["recommendations"] if r["route"].key == route_key
                        and (r["account"].id if r["account"] else 0) == account_id), None)
            if row is None:
                raise ValueError("La ruta ya no está en el mercado reciente. Actualiza la lista.")
            route, account = row["route"], row["account"]
            _queue().enqueue({"route_key": route.key, "target": target_dict(row["target"]),
                              "funds": str(account.available if account else row["target"].max_fiat),
                              "buy_method": route.buy_method, "sell_method": route.sell_method,
                              "old_buy_price": str(route.buy.price), "old_sell_price": str(route.sell.price),
                              "account_id": account_id, "funds_revision": ctx["funds_revision"]})
            return _TEMPLATES.TemplateResponse(request, "partials/market.html",
                                               _market_context(request, message="Comprobación solicitada. El resultado aparecerá aquí."))
        except ValueError as exc:
            return _TEMPLATES.TemplateResponse(request, "partials/market.html", _market_context(request, error=str(exc)))
        except sqlite3.Error:
            return _TEMPLATES.TemplateResponse(request, "partials/market.html", _market_context(request, error="No se pudo guardar la solicitud."))

    @app.post("/market/reserve", response_class=HTMLResponse)
    def reserve_route(request: Request, route_key: str = Form(...), account_id: int = Form(...),
                      version: int = Form(...), funds_revision: int = Form(...),
                      _: None = Depends(auth.require_login)) -> HTMLResponse:
        ctx = _market_context(request)
        try:
            row = next((r for r in ctx["recommendations"] if r["route"].key == route_key
                        and r["account"] and r["account"].id == account_id), None)
            if row is None or not row["revalidated"]:
                raise ValueError("Revalida esta ruta con los fondos actuales antes de reservar.")
            _funds().reserve(account_id, str(row["route"].amount_fiat), expected_version=version,
                            expected_revision=funds_revision, note=f"{row['target'].label}: {row['route'].buy_method} → {row['route'].sell_method}",
                            source_request=row["request"]["id"])
            return _TEMPLATES.TemplateResponse(request, "partials/market.html",
                                               _market_context(request, message="Fondos reservados. Puedes liberarlos desde Fondos."))
        except ValueError as exc:
            return _TEMPLATES.TemplateResponse(request, "partials/market.html", _market_context(request, error=str(exc)))

    @app.get("/partials/status", response_class=HTMLResponse)
    def partial_status(request: Request) -> HTMLResponse:
        p = _paths()
        ctx = {
            "request": request,
            "authed": auth.is_authed(request),
            "bot": _bot_view(),
            "cfg": env_store.read_config(),
            "stats": db_reader.stats_24h(p["db_path"]),
            "status": db_reader.read_status(p["status_path"]),
        }
        return _TEMPLATES.TemplateResponse(request, "partials/status.html", ctx)

    @app.get("/partials/opportunities", response_class=HTMLResponse)
    def partial_opportunities(request: Request) -> HTMLResponse:
        p = _paths()
        ctx = {
            "request": request,
            # Sin esto los botones de registro desaparecerían al primer repintado.
            "authed": auth.is_authed(request),
            "opportunities": db_reader.recent_opportunities(p["db_path"], limit=50),
        }
        return _TEMPLATES.TemplateResponse(request, "partials/opportunities.html", ctx)

    @app.get("/partials/trade-amount", response_class=HTMLResponse)
    def partial_trade_amount(request: Request) -> HTMLResponse:
        p = _paths()
        latest = db_reader.recent_opportunities(p["db_path"], limit=1)
        ctx = {"request": request, "opportunity": latest[0] if latest else None}
        return _TEMPLATES.TemplateResponse(request, "partials/trade_amount.html", ctx)

    @app.get("/partials/log", response_class=HTMLResponse)
    def partial_log(request: Request) -> HTMLResponse:
        p = _paths()
        ctx = {
            "request": request,
            "log_lines": db_reader.tail_log(p["log_path"], 120, newest_first=True),
        }
        return _TEMPLATES.TemplateResponse(request, "partials/log.html", ctx)

    # --- login ----------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "configured": auth.password_configured(),
                "error": None,
            },
        )

    @app.post("/login")
    def login_submit(request: Request, password: str = Form("")) -> RedirectResponse:
        if auth.verify_password(password):
            auth.login_session(request)
            return RedirectResponse("/", status_code=303)
        return _TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "configured": auth.password_configured(),
                "error": "Contraseña incorrecta."
                if auth.password_configured()
                else "DASHBOARD_PASSWORD no está configurada en el servidor.",
            },
            status_code=401,
        )

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        auth.logout_session(request)
        return RedirectResponse("/", status_code=303)

    # --- control del bot (login) ----------------------------------------

    @app.post("/bot/start", response_class=HTMLResponse)
    def bot_start(request: Request, _: None = Depends(auth.require_login)) -> HTMLResponse:
        bot.start()
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/bot_controls.html",
            {"request": request, "authed": True, "bot": _bot_view()},
        )

    @app.post("/bot/stop", response_class=HTMLResponse)
    def bot_stop(request: Request, _: None = Depends(auth.require_login)) -> HTMLResponse:
        bot.stop()
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/bot_controls.html",
            {"request": request, "authed": True, "bot": _bot_view()},
        )

    @app.post("/bot/restart", response_class=HTMLResponse)
    def bot_restart(request: Request, _: None = Depends(auth.require_login)) -> HTMLResponse:
        bot.restart()
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/bot_controls.html",
            {"request": request, "authed": True, "bot": _bot_view()},
        )

    # --- limpiar oportunidades (login) ----------------------------------

    @app.post("/opportunities/clear", response_class=HTMLResponse)
    def opportunities_clear(
        request: Request, _: None = Depends(auth.require_login)
    ) -> HTMLResponse:
        p = _paths()
        db_reader.clear_opportunities(p["db_path"])
        # También el status.json: reinicia el "mejor spread" y la última oportunidad.
        # El botón dispara además un refresh de #status-panel (KPIs de 24 h y spread).
        db_reader.clear_status(p["status_path"])
        # Devuelve la tabla ya vacía para que HTMX la reemplace en el acto.
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/opportunities.html",
            {
                "request": request,
                "authed": True,
                "opportunities": db_reader.recent_opportunities(p["db_path"], limit=50),
            },
        )

    # --- registro de operaciones (login) --------------------------------
    #
    # Todo va detrás de login, incluida la LECTURA: el dashboard corre en un
    # dominio público con la supervisión abierta, y aquí hay precios reales,
    # contrapartes y notas del usuario.

    def _as_float(value: str) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _opp_snapshot(opp_id: int) -> dict:
        """Oportunidad con la que se prellena el formulario.

        Solo se usa al ABRIR el formulario; el POST recibe el snapshot ya
        resuelto en campos ocultos. Devuelve {} si no está (p. ej. si el usuario
        pulsó Limpiar entre medias): el formulario sale vacío, no revienta.
        """
        p = _paths()
        for o in db_reader.recent_opportunities(p["db_path"], limit=50):
            if o.get("id") == opp_id:
                return o
        return {}

    def _trades_ctx(request: Request) -> dict:
        p = _paths()
        return {
            "request": request,
            "authed": True,
            "trades": trade_store.recent_trades(p["trades_db_path"], limit=50),
            "summary": trade_store.summary(p["trades_db_path"]),
        }

    def _market_snapshot(request: Request, market_key: str, account_id: int) -> dict:
        ctx = _market_context(request)
        row = next((r for r in ctx["recommendations"] if r["route"].key == market_key
                    and (r["account"].id if r["account"] else 0) == account_id), None)
        held = None
        for reservation in _funds().reservations():
            if reservation["account_id"] != account_id or not reservation["source_request"]:
                continue
            candidate = _queue().get(reservation["source_request"])
            if candidate and candidate["payload"]["route_key"] == market_key:
                held = candidate
                break
        if held and held["result"] and held["result"].get("route"):
            # Reservar reduce el disponible, pero no cambia lo que se iba a operar.
            result = held["result"]
            route = read_route(result["route"])
            target = read_target(result["target"])
            stamp = result["checked_at"]
            snapshot_revision = held["payload"]["funds_revision"]
            revalidation_id = held["id"]
            feasible = True
        else:
            if row is None:
                return {}
            route, target = row["route"], row["target"]
            result = row["request"]["result"] if row["revalidated"] else None
            stamp = result["checked_at"] if result else next(
                b["checked_at"] for b in ctx["books"] if b["target"].label == target.label)
            snapshot_revision = ctx["funds_revision"]
            revalidation_id = row["request"]["id"] if result else None
            feasible = row["reason"] == "compatible"
        return {"detected_at": stamp, "detected_local": db_reader.to_local(stamp),
                "asset": target.asset, "fiat": target.fiat,
                "net_pct": route.net_pct, "est_profit_usdt": route.units * route.net_pct / 100,
                "buy_price": route.buy.price, "sell_price": route.sell.price,
                "usable_usdt": route.units, "usable_fiat_buy": route.amount_fiat,
                "usable_fiat_sell": route.units * route.sell.price, "feasible": feasible,
                "buy_advertiser": route.buy.advertiser_name, "sell_advertiser": route.sell.advertiser_name,
                "buy_pay_method": route.buy_method, "sell_pay_method": route.sell_method,
                "market_route_key": market_key, "funds_revision": snapshot_revision,
                "revalidation_id": revalidation_id,
                "revalidation_checked_at": stamp if result else None}

    @app.get("/partials/trades", response_class=HTMLResponse)
    def partial_trades(
        request: Request, _: None = Depends(auth.require_login)
    ) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(request, "partials/trades.html", _trades_ctx(request))

    @app.get("/trades/new", response_class=HTMLResponse)
    def trade_new(
        request: Request, opp_id: int = 0, market_key: str = "", account_id: int = 0,
        _: None = Depends(auth.require_login)
    ) -> HTMLResponse:
        o = _market_snapshot(request, market_key, account_id) if market_key else _opp_snapshot(opp_id)
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/trade_form.html",
            {"request": request, "opp": o, "opp_id": opp_id},
        )

    @app.get("/trades/fail", response_class=HTMLResponse)
    def trade_fail(
        request: Request, opp_id: int = 0, market_key: str = "", account_id: int = 0,
        _: None = Depends(auth.require_login)
    ) -> HTMLResponse:
        o = _market_snapshot(request, market_key, account_id) if market_key else _opp_snapshot(opp_id)
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/trade_fail_form.html",
            {
                "request": request,
                "opp": o,
                "opp_id": opp_id,
                "reasons": trade_store.FAILURE_REASONS,
            },
        )

    @app.post("/trades", response_class=HTMLResponse)
    def trade_save(
        request: Request,
        status: str = Form(...),
        opportunity_id: int = Form(...),
        # El snapshot viaja en el formulario (ver _opp_snapshot_fields.html): no se
        # relee la DB del bot aquí, porque una lectura bloqueada guardaría la
        # operación sin estimado y sin comparación posible.
        detected_at: str = Form(""),
        asset: str = Form(""),
        fiat: str = Form(""),
        est_net_pct: str = Form(""),
        est_profit_usdt: str = Form(""),
        real_buy_price: str = Form(""),
        real_sell_price: str = Form(""),
        real_usdt: str = Form(""),
        real_fiat_buy: str = Form(""),
        real_fiat_sell: str = Form(""),
        buy_advertiser: str = Form(""),
        sell_advertiser: str = Form(""),
        buy_pay_method: str = Form(""),
        sell_pay_method: str = Form(""),
        failure_reason: str = Form(""),
        notes: str = Form(""),
        market_route_key: str = Form(""),
        funds_revision: str = Form(""),
        revalidation_id: str = Form(""),
        revalidation_checked_at: str = Form(""),
        _: None = Depends(auth.require_login),
    ) -> HTMLResponse:
        p = _paths()
        trade_store.save_trade(
            p["trades_db_path"],
            {
                "status": (
                    trade_store.STATUS_FAILED
                    if status == trade_store.STATUS_FAILED
                    else trade_store.STATUS_COMPLETED
                ),
                "opportunity_id": opportunity_id,
                "detected_at": detected_at or None,
                "asset": asset or None,
                "fiat": fiat or None,
                "est_net_pct": _as_float(est_net_pct),
                "est_profit_usdt": est_profit_usdt or None,
                "real_buy_price": real_buy_price,
                "real_sell_price": real_sell_price,
                "real_usdt": real_usdt,
                "real_fiat_buy": real_fiat_buy,
                "real_fiat_sell": real_fiat_sell,
                "buy_advertiser": buy_advertiser or None,
                "sell_advertiser": sell_advertiser or None,
                "buy_pay_method": buy_pay_method or None,
                "sell_pay_method": sell_pay_method or None,
                "failure_reason": failure_reason or None,
                "notes": notes or None,
                "market_route_key": market_route_key or None,
                "funds_revision": funds_revision or None,
                "revalidation_id": revalidation_id or None,
                "revalidation_checked_at": revalidation_checked_at or None,
            },
        )
        return _TEMPLATES.TemplateResponse(request, "partials/trades.html", _trades_ctx(request))

    # --- configuración (login) ------------------------------------------

    async def _discover(cfg: dict) -> list[tuple[str, str]]:
        """Combina los métodos de los anuncios de todas las monedas elegidas."""
        methods: dict[str, str] = {}
        for asset in dict.fromkeys(cfg.get("assets") or ["USDT"]):
            try:
                methods.update(await env_store.discover_methods(asset, cfg["fiat"]))
            except Exception:  # sin red en un par, conserva los otros
                continue
        return sorted(methods.items(), key=lambda item: item[0].lower())

    @app.get("/config", response_class=HTMLResponse)
    async def config_form(
        request: Request, _: None = Depends(auth.require_login)
    ) -> HTMLResponse:
        cfg = env_store.read_config()
        methods = await _discover(cfg)
        return _TEMPLATES.TemplateResponse(
            request,
            "config.html",
            {
                "request": request,
                "authed": True,
                "cfg": cfg,
                "methods": methods,
                "selected": set(cfg["pay_methods"]),
                "selected_assets": set(cfg["assets"]),
                "message": None,
                "error": None,
            },
        )

    @app.post("/config", response_class=HTMLResponse)
    async def config_submit(
        request: Request,
        _: None = Depends(auth.require_login),
        threshold_pct: str = Form(...),
        max_fiat: str = Form(...),
        poll_interval_s: int = Form(...),
        pay_methods: list[str] = Form(default=[]),
        assets: list[str] = Form(default=[]),
    ) -> HTMLResponse:
        p = _paths()
        cfg = env_store.read_config()
        message = error = None
        try:
            env_store.write_config(
                p["env_path"],
                threshold_pct=threshold_pct,
                max_fiat=max_fiat,
                pay_methods=pay_methods,
                poll_interval_s=poll_interval_s,
                assets=assets,
            )
            if bot.is_running():
                bot.restart()
                message = "Configuración guardada y bot reiniciado."
            else:
                message = "Configuración guardada (el bot está detenido)."
            cfg = env_store.read_config()
        except (ValueError, OSError) as exc:
            error = f"No se pudo guardar: {exc}"

        methods = await _discover(cfg)
        return _TEMPLATES.TemplateResponse(
            request,
            "config.html",
            {
                "request": request,
                "authed": True,
                "cfg": cfg,
                "methods": methods,
                "selected": set(cfg["pay_methods"]),
                # Tras un error se re-marcan las del formulario, no las guardadas:
                # así el usuario no pierde lo que había elegido.
                "selected_assets": set(a.upper() for a in assets) if error else set(cfg["assets"]),
                "message": message,
                "error": error,
            },
        )

    return app


app = create_app()
