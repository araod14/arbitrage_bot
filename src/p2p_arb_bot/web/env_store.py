"""Lectura/escritura de los parámetros clave del bot en el fichero ``.env``.

La escritura preserva el resto de líneas y comentarios del ``.env``: solo
reemplaza (o añade) las claves gestionadas por el dashboard. Los valores actuales
se leen reutilizando ``Defaults.from_env`` para no duplicar el parseo/casting.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
from decimal import Decimal

from ..config import SUPPORTED_ASSETS, Defaults
from ..infrastructure.binance_p2p import BinanceP2PSource
from ..infrastructure.discovery import discover_pay_methods

logger = logging.getLogger(__name__)

#: Claves que el dashboard permite editar (parámetros clave).
MANAGED_KEYS = ("THRESHOLD_PCT", "MAX_FIAT", "PAY_METHODS", "POLL_INTERVAL_S", "ASSETS")


def read_config() -> dict:
    """Valores actuales de los parámetros clave (desde entorno/``.env``)."""
    d = Defaults.from_env()
    return {
        "threshold_pct": str(d.threshold_pct),
        "max_fiat": str(d.max_fiat),
        "pay_methods": list(d.pay_methods),
        "poll_interval_s": d.poll_interval_s,
        "assets": list(d.assets),
        "supported_assets": list(SUPPORTED_ASSETS),
        "fiat": d.fiat,
    }


def write_config(
    env_path: str,
    *,
    threshold_pct: str,
    max_fiat: str,
    pay_methods: list[str],
    poll_interval_s: int,
    assets: list[str],
) -> None:
    """Actualiza solo las claves gestionadas en ``.env``, preservando el resto.

    Valida los numéricos antes de escribir (lanza ``ValueError`` si no parsean).
    """
    threshold = Decimal(threshold_pct)  # valida
    amount = Decimal(max_fiat)          # valida
    if amount <= 0:
        raise ValueError("El fondo disponible debe ser > 0.")
    if int(poll_interval_s) <= 0:
        raise ValueError("El intervalo de polling debe ser > 0.")

    chosen = tuple(dict.fromkeys(a.strip().upper() for a in assets if a.strip()))
    if not chosen:
        raise ValueError("Marca al menos una moneda a monitorear.")
    # El POST podría venir manipulado: solo se aceptan monedas del catálogo.
    unknown = [a for a in chosen if a not in SUPPORTED_ASSETS]
    if unknown:
        raise ValueError(f"Moneda no soportada: {', '.join(unknown)}.")

    new_values = {
        "THRESHOLD_PCT": str(threshold),
        "MAX_FIAT": str(amount),
        "PAY_METHODS": ",".join(m.strip() for m in pay_methods if m.strip()),
        "POLL_INTERVAL_S": str(int(poll_interval_s)),
        "ASSETS": ",".join(chosen),
    }

    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()

    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in new_values:
                out.append(f"{key}={new_values[key]}")
                seen.add(key)
                continue
        out.append(line)

    for key, value in new_values.items():
        if key not in seen:
            out.append(f"{key}={value}")

    content = "\n".join(out) + "\n"
    tmp = f"{env_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    try:
        os.replace(tmp, env_path)
    except OSError as exc:
        # En Docker el .env suele montarse como bind-mount de un fichero único;
        # entonces el destino es un punto de montaje y no se puede renombrar
        # encima (EBUSY), ni mover entre dispositivos distintos (EXDEV). En esos
        # casos escribimos in-place sobre el inodo ya montado.
        if exc.errno not in (errno.EBUSY, errno.EXDEV):
            raise
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.unlink(tmp)

    # Actualiza el entorno del propio dashboard para que el subproceso del bot
    # (que hereda os.environ) tome los nuevos valores al reiniciar. Necesario en
    # Docker, donde las vars ya están en el entorno y load_dotenv no las pisa.
    os.environ.update(new_values)
    logger.info("Configuración guardada en %s", env_path)


async def discover_methods(
    asset: str, fiat: str, *, timeout_s: float = 10.0
) -> list[tuple[str, str]]:
    """Métodos de pago disponibles para poblar el formulario de config.

    Acota el descubrimiento con un timeout para que la página no se quede colgada
    si Binance está lento o inalcanzable (el caller cae a lista vacía).
    """
    source = BinanceP2PSource(
        impersonate=os.getenv("IMPERSONATE", "chrome") or "chrome",
        proxy=os.getenv("PROXY") or None,
    )
    try:
        return await asyncio.wait_for(
            discover_pay_methods(source, asset, fiat), timeout=timeout_s
        )
    finally:
        await source.aclose()
