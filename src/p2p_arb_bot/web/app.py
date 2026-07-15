"""App FastAPI del dashboard: wiring de rutas, sesiones, plantillas y estado.

Punto de entrada del proceso web. No importa ``MonitorService``: controla el bot
como subproceso (``BotManager``) y lee su salida (DB, status.json, log) en modo
solo-lectura. Así el dashboard y el bot quedan desacoplados.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db_reader, env_store, trade_store
from .bot_manager import BotManager

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


def create_app() -> FastAPI:
    # Carga .env para que DASHBOARD_PASSWORD/SECRET y las rutas estén disponibles
    # vía os.getenv, igual que hace el bot en Defaults.from_env(). Sin esto, el
    # login diría que la contraseña no está configurada aunque esté en el .env.
    load_dotenv(os.getenv("ENV_PATH") or None)

    app = FastAPI(title="P2P Arb Dashboard")

    _TEMPLATES.env.filters["miles"] = _fmt_miles

    secret = os.getenv("DASHBOARD_SECRET") or secrets.token_hex(32)
    app.add_middleware(SessionMiddleware, secret_key=secret)

    static_dir = _HERE / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    paths = _paths()
    bot = BotManager(
        pidfile=os.getenv("BOT_PIDFILE")
        or os.path.join(os.path.dirname(os.path.abspath(paths["db_path"])), "bot.pid"),
        log_path=paths["log_path"],
    )

    # --- helpers de contexto --------------------------------------------

    def _dashboard_context(request: Request) -> dict:
        p = _paths()
        return {
            "request": request,
            "authed": auth.is_authed(request),
            "bot": _bot_view(),
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

    @app.get("/partials/status", response_class=HTMLResponse)
    def partial_status(request: Request) -> HTMLResponse:
        p = _paths()
        ctx = {
            "request": request,
            "authed": auth.is_authed(request),
            "bot": _bot_view(),
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

    @app.get("/partials/trades", response_class=HTMLResponse)
    def partial_trades(
        request: Request, _: None = Depends(auth.require_login)
    ) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(request, "partials/trades.html", _trades_ctx(request))

    @app.get("/trades/new", response_class=HTMLResponse)
    def trade_new(
        request: Request, opp_id: int, _: None = Depends(auth.require_login)
    ) -> HTMLResponse:
        o = _opp_snapshot(opp_id)
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/trade_form.html",
            {"request": request, "opp": o, "opp_id": opp_id},
        )

    @app.get("/trades/fail", response_class=HTMLResponse)
    def trade_fail(
        request: Request, opp_id: int, _: None = Depends(auth.require_login)
    ) -> HTMLResponse:
        o = _opp_snapshot(opp_id)
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
            },
        )
        return _TEMPLATES.TemplateResponse(request, "partials/trades.html", _trades_ctx(request))

    # --- configuración (login) ------------------------------------------

    @app.get("/config", response_class=HTMLResponse)
    async def config_form(
        request: Request, _: None = Depends(auth.require_login)
    ) -> HTMLResponse:
        cfg = env_store.read_config()
        try:
            methods = await env_store.discover_methods(cfg["asset"], cfg["fiat"])
        except Exception:  # noqa: BLE001 — sin red no debe romper el form
            methods = []
        return _TEMPLATES.TemplateResponse(
            request,
            "config.html",
            {
                "request": request,
                "authed": True,
                "cfg": cfg,
                "methods": methods,
                "selected": set(cfg["pay_methods"]),
                "message": None,
                "error": None,
            },
        )

    @app.post("/config", response_class=HTMLResponse)
    async def config_submit(
        request: Request,
        _: None = Depends(auth.require_login),
        threshold_pct: str = Form(...),
        max_usdt: str = Form(...),
        poll_interval_s: int = Form(...),
        pay_methods: list[str] = Form(default=[]),
    ) -> HTMLResponse:
        p = _paths()
        cfg = env_store.read_config()
        message = error = None
        try:
            env_store.write_config(
                p["env_path"],
                threshold_pct=threshold_pct,
                max_usdt=max_usdt,
                pay_methods=pay_methods,
                poll_interval_s=poll_interval_s,
            )
            if bot.is_running():
                bot.restart()
                message = "Configuración guardada y bot reiniciado."
            else:
                message = "Configuración guardada (el bot está detenido)."
            cfg = env_store.read_config()
        except (ValueError, OSError) as exc:
            error = f"No se pudo guardar: {exc}"

        try:
            methods = await env_store.discover_methods(cfg["asset"], cfg["fiat"])
        except Exception:  # noqa: BLE001
            methods = []
        return _TEMPLATES.TemplateResponse(
            request,
            "config.html",
            {
                "request": request,
                "authed": True,
                "cfg": cfg,
                "methods": methods,
                "selected": set(cfg["pay_methods"]),
                "message": message,
                "error": error,
            },
        )

    return app


app = create_app()
