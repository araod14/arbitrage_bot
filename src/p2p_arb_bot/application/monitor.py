"""Caso de uso: ciclo buscar -> analizar -> persistir -> notificar.

Depende solo de los puertos (``MarketDataSource``, ``OpportunityRepository``,
``Notifier``), nunca de implementaciones concretas. Los adaptadores se inyectan
por constructor desde el composition root (``main.py``).
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from ..domain.arbitrage import best_spread, find_best_opportunity
from ..domain.models import Ad, Opportunity, TradeType, WatchTarget
from .ports import MarketDataSource, Notifier, OpportunityRepository

logger = logging.getLogger(__name__)


class MonitorService:
    """Orquesta la vigilancia de una lista de ``WatchTarget``.

    Acepta *listas* de repositorios y notificadores: así se pueden combinar
    varios adaptadores a la vez (p. ej. consola + SQLite) sin tocar este código.
    """

    def __init__(
        self,
        source: MarketDataSource,
        repositories: list[OpportunityRepository],
        notifiers: list[Notifier],
        *,
        poll_interval_s: float = 30.0,
        jitter_frac: float = 0.25,
        dedup_window_s: int = 60,
        max_concurrency: int = 6,
    ) -> None:
        self._source = source
        self._repositories = repositories
        self._notifiers = notifiers
        self._poll_interval_s = poll_interval_s
        self._jitter_frac = jitter_frac
        self._dedup_window_s = dedup_window_s
        self._sem = asyncio.Semaphore(max_concurrency)

    async def _fetch(self, target: WatchTarget, trade_type: TradeType) -> list[Ad]:
        async with self._sem:  # limita la concurrencia total contra el endpoint
            return await self._source.fetch_ads(
                asset=target.asset,
                fiat=target.fiat,
                trade_type=trade_type,
                pay_types=list(target.pay_methods),
                merchant_check=target.merchant_check,
                rows=target.rows,
            )

    async def run_cycle(self, target: WatchTarget) -> Opportunity | None:
        """Ejecuta un ciclo para un target. Devuelve la oportunidad si la notificó."""
        # Ambos lados en paralelo (y, con varios targets, también entre sí).
        buy_ads, sell_ads = await asyncio.gather(
            self._fetch(target, "BUY"),
            self._fetch(target, "SELL"),
        )

        now = datetime.now(timezone.utc)
        opp = find_best_opportunity(buy_ads, sell_ads, target, now)

        if opp is None:
            spread = best_spread(buy_ads, sell_ads, target)
            await self._heartbeat(target, spread)
            return None

        if self._is_recent_duplicate(opp):
            logger.debug(
                "Oportunidad duplicada suprimida (%s): net=%.3f%%",
                target.label,
                opp.net_pct,
            )
            return None

        for repo in self._repositories:
            repo.save(opp)
        for notifier in self._notifiers:
            await notifier.notify_opportunity(opp)
        return opp

    def _is_recent_duplicate(self, opp: Opportunity) -> bool:
        return any(
            repo.recent_duplicate(opp, self._dedup_window_s)
            for repo in self._repositories
        )

    async def _heartbeat(self, target: WatchTarget, spread) -> None:
        for notifier in self._notifiers:
            await notifier.notify_heartbeat(target, spread)

    async def run_forever(self, targets: list[WatchTarget]) -> None:
        """Bucle principal: itera targets y duerme con jitter entre ciclos."""
        while True:
            for target in targets:
                try:
                    await self.run_cycle(target)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — un fallo no debe matar el bucle
                    logger.exception("Fallo en el ciclo de %s", target.label)
                    await self._heartbeat(target, None)
            await self._sleep_with_jitter()

    async def _sleep_with_jitter(self) -> None:
        jitter = self._poll_interval_s * self._jitter_frac
        delay = max(1.0, self._poll_interval_s + random.uniform(-jitter, jitter))
        await asyncio.sleep(delay)
