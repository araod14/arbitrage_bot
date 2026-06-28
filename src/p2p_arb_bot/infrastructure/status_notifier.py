"""Adaptador ``Notifier`` que vuelca el último estado del bot a un JSON.

No reemplaza a otros notifiers: se añade a la lista de ``MonitorService`` junto
al ``ConsoleNotifier``. Su único cometido es dejar en disco un pequeño fichero de
estado (``status.json``) que el dashboard web pueda leer para mostrar, sin pegarle
a Binance por su cuenta, el último heartbeat y la última oportunidad detectada.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal

from ..domain.models import Opportunity, WatchTarget

logger = logging.getLogger(__name__)


class StatusNotifier:
    """Persiste el último estado del bot en un JSON con escritura atómica."""

    def __init__(self, status_path: str = "status.json") -> None:
        self._path = status_path
        self._last_opportunity: dict | None = None

    async def notify_heartbeat(
        self, target: WatchTarget, best_spread: Decimal | None
    ) -> None:
        self._write(
            {
                "target": target.label,
                "best_net_pct": float(best_spread) if best_spread is not None else None,
            }
        )

    async def notify_opportunity(self, opp: Opportunity) -> None:
        self._last_opportunity = {
            "detected_at": opp.detected_at.astimezone().isoformat(),
            "asset": opp.asset,
            "fiat": opp.fiat,
            "buy_price": str(opp.buy_price),
            "sell_price": str(opp.sell_price),
            "buy_pay_method": opp.buy_pay_method,
            "sell_pay_method": opp.sell_pay_method,
            "net_pct": float(opp.net_pct),
            "spread_pct": float(opp.spread_pct),
            "est_profit_usdt": str(opp.est_profit_usdt),
        }
        self._write(
            {
                "target": f"{opp.asset}/{opp.fiat}",
                "best_net_pct": float(opp.net_pct),
            }
        )

    def _write(self, extra: dict) -> None:
        payload = {
            "last_update": datetime.now(timezone.utc).astimezone().isoformat(),
            "last_opportunity": self._last_opportunity,
            **extra,
        }
        try:
            directory = os.path.dirname(os.path.abspath(self._path))
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                os.replace(tmp, self._path)  # rename atómico
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        except OSError as exc:  # no debe tumbar el ciclo del bot
            logger.warning("No se pudo escribir el estado en %s: %s", self._path, exc)
