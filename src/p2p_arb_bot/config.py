"""Configuración tipada del bot, cargada de ``.env`` y validada al arranque.

Los valores del ``.env`` actúan como *predeterminados*; en modo interactivo el
bot los ofrece como default en cada prompt (Enter = aceptar el default).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv

from .domain.models import WatchTarget

logger = logging.getLogger(__name__)


def validate_storage_paths(db_path: str, control_db_path: str, trades_db_path: str) -> None:
    """Las bases pública, privada y de control deben ser ficheros distintos."""
    paths = [os.path.realpath(p) for p in (db_path, control_db_path, trades_db_path)]
    if len(set(paths)) != 3:
        raise ValueError("DB_PATH, CONTROL_DB_PATH y TRADES_DB_PATH deben ser ficheros distintos.")

#: Criptos que ofrece el mercado P2P de Binance y que el bot sabe vigilar. Es
#: catálogo de configuración (alimenta el prompt interactivo y los checkboxes del
#: dashboard), no lógica de dominio: el motor vigila cualquier asset que le llegue
#: en un ``WatchTarget``. Ampliar la lista es añadir un elemento aquí.
SUPPORTED_ASSETS: tuple[str, ...] = ("USDT", "BTC", "BNB", "ETH", "FDUSD", "DAI")


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "si", "sí", "on")


def _get_decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default) or default
    try:
        return Decimal(raw)
    except InvalidOperation:
        return Decimal(default)


def _get_assets() -> tuple[str, ...]:
    """Criptos a vigilar, de ``ASSETS`` (CSV).

    Acepta el antiguo ``ASSET`` (una sola) como alias legado para no romper los
    ``.env`` ya escritos. Normaliza a mayúsculas y deduplica preservando el orden.
    """
    raw = os.getenv("ASSETS") or os.getenv("ASSET") or "USDT"
    names = (a.strip().upper() for a in raw.split(","))
    assets = tuple(dict.fromkeys(a for a in names if a))
    return assets or ("USDT",)


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(slots=True)
class Defaults:
    """Valores predeterminados leídos del entorno/``.env``."""

    assets: tuple[str, ...] = ("USDT",)
    fiat: str = "VES"
    pay_methods: tuple[str, ...] = ()
    max_fiat: Decimal = Decimal("5000")
    threshold_pct: Decimal = Decimal("1.0")
    fee_buffer_pct: Decimal = Decimal("0.0")
    outlier_max_dev_pct: Decimal = Decimal("15")
    merchant_check: bool = False
    poll_interval_s: int = 30
    db_path: str = "opportunities.db"
    log_path: str = "p2p_arb_bot.log"
    status_path: str = "status.json"
    screenshots_dir: str = "screenshots"
    screenshots_enabled: bool = False
    impersonate: str = "chrome"
    proxy: str | None = None
    beep: bool = True
    no_input: bool = False
    rows: int = 20
    market_freshness_s: int = 60
    market_retention_days: int = 7
    market_max_routes: int = 50
    control_db_path: str = "control.db"
    revalidation_ttl_s: int = 120
    revalidation_cooldown_s: int = 10
    revalidation_max_pending: int = 10

    @classmethod
    def from_env(cls) -> "Defaults":
        load_dotenv(os.getenv("ENV_PATH") or None)
        if os.getenv("MAX_USDT") and not os.getenv("MAX_FIAT"):
            # Un .env de antes del cambio a fondo en fiat: sin este aviso el bot
            # arrancaría con el default de MAX_FIAT, que no tiene nada que ver con
            # el fondo que el usuario cree tener configurado.
            logger.warning(
                "MAX_USDT ya no se lee: el fondo ahora va en fiat (MAX_FIAT). "
                "Se usa MAX_FIAT=%s; define MAX_FIAT en tu .env para ajustarlo.",
                os.getenv("MAX_FIAT") or "5000",
            )
        methods_raw = os.getenv("PAY_METHODS", "") or ""
        methods = tuple(m.strip() for m in methods_raw.split(",") if m.strip())
        return cls(
            assets=_get_assets(),
            fiat=os.getenv("FIAT", "VES") or "VES",
            pay_methods=methods,
            max_fiat=_get_decimal("MAX_FIAT", "5000"),
            threshold_pct=_get_decimal("THRESHOLD_PCT", "1.0"),
            fee_buffer_pct=_get_decimal("FEE_BUFFER_PCT", "0.0"),
            outlier_max_dev_pct=_get_decimal("OUTLIER_MAX_DEV_PCT", "2.5"),
            merchant_check=_get_bool("MERCHANT_CHECK", False),
            poll_interval_s=_get_int("POLL_INTERVAL_S", 30),
            db_path=os.getenv("DB_PATH", "opportunities.db") or "opportunities.db",
            log_path=os.getenv("LOG_PATH", "p2p_arb_bot.log") or "p2p_arb_bot.log",
            status_path=os.getenv("STATUS_PATH", "status.json") or "status.json",
            screenshots_dir=os.getenv("SCREENSHOTS_DIR", "screenshots") or "screenshots",
            screenshots_enabled=_get_bool("SCREENSHOTS", False),
            impersonate=os.getenv("IMPERSONATE", "chrome") or "chrome",
            proxy=os.getenv("PROXY") or None,
            beep=_get_bool("BEEP", True),
            no_input=_get_bool("NO_INPUT", False),
            rows=_get_int("ROWS", 20),
            market_freshness_s=max(1, _get_int("MARKET_FRESHNESS_S", 2 * max(1, _get_int("POLL_INTERVAL_S", 30)))),
            market_retention_days=max(1, _get_int("MARKET_RETENTION_DAYS", 7)),
            market_max_routes=max(1, _get_int("MARKET_MAX_ROUTES", 50)),
            control_db_path=os.getenv("CONTROL_DB_PATH") or os.path.join(
                os.path.dirname(os.getenv("DB_PATH") or "opportunities.db"), "control.db"),
            revalidation_ttl_s=max(1, _get_int("REVALIDATION_TTL_S", 120)),
            revalidation_cooldown_s=max(0, _get_int("REVALIDATION_COOLDOWN_S", 10)),
            revalidation_max_pending=max(1, _get_int("REVALIDATION_MAX_PENDING", 10)),
        )


@dataclass(slots=True)
class AppConfig:
    """Configuración final ya resuelta y validada que consume ``main``."""

    targets: list[WatchTarget]
    poll_interval_s: int
    db_path: str
    log_path: str
    status_path: str
    screenshots_dir: str
    screenshots_enabled: bool
    impersonate: str
    proxy: str | None
    beep: bool
    rows: int = 20

    def validate(self) -> None:
        if not self.targets:
            raise ValueError("Debe haber al menos un WatchTarget configurado.")
        if self.poll_interval_s <= 0:
            raise ValueError("POLL_INTERVAL_S debe ser > 0.")
        for t in self.targets:
            if not t.max_fiat.is_finite() or t.max_fiat <= 0:
                raise ValueError(f"max_fiat debe ser > 0 (target {t.label}).")
            if not all(v.is_finite() for v in (t.threshold_pct, t.fee_buffer_pct, t.outlier_max_dev_pct)):
                raise ValueError("Los porcentajes deben ser finitos.")
            if not t.asset or not t.fiat:
                raise ValueError("asset y fiat son obligatorios.")
