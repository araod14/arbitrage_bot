"""Caso de uso: ciclo buscar -> analizar -> persistir -> notificar.

Depende solo de los puertos (``MarketDataSource``, ``OpportunityRepository``,
``Notifier``), nunca de implementaciones concretas. Los adaptadores se inyectan
por constructor desde el composition root (``main.py``).
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import asdict
from typing import Callable
from datetime import datetime, timezone

from ..domain.arbitrage import best_spread, find_best_opportunity
from ..domain.models import Ad, Opportunity, TradeType, WatchTarget
from ..domain.market import decimal_amount, evaluate_routes
from .ports import MarketDataSource, Notifier, OpportunityRepository, MarketObserver, RevalidationQueue

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
        observer: MarketObserver | None = None,
        revalidations: RevalidationQueue | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._repositories = repositories
        self._notifiers = notifiers
        self._poll_interval_s = poll_interval_s
        self._jitter_frac = jitter_frac
        self._dedup_window_s = dedup_window_s
        self._sem = asyncio.Semaphore(max_concurrency)
        self._observer = observer
        self._revalidations = revalidations
        self._clock = clock or (lambda: datetime.now(timezone.utc))

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
        try:
            buy_ads, sell_ads = await self._fetch_book(target)
        except Exception:
            if self._observer:
                self._observer.failed(target, self._clock())
            raise

        now = self._clock()
        if self._observer:
            self._observer.record(target, buy_ads, sell_ads, now)
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
            await self._heartbeat(target, opp.net_pct)
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
                await self.process_revalidation(targets)
            await self._sleep_with_jitter(targets)

    async def _sleep_with_jitter(self, targets: list[WatchTarget] | None = None) -> None:
        jitter = self._poll_interval_s * self._jitter_frac
        delay = max(1.0, self._poll_interval_s + random.uniform(-jitter, jitter))
        if self._revalidations is None:
            await asyncio.sleep(delay)
            return
        # Atender la cola durante la espera sin aumentar el polling de mercado.
        deadline = asyncio.get_running_loop().time() + delay
        while asyncio.get_running_loop().time() < deadline:
            await self.process_revalidation(targets or [])
            await asyncio.sleep(min(1.0, max(0, deadline - asyncio.get_running_loop().time())))

    async def _fetch_book(self, target: WatchTarget) -> tuple[list[Ad], list[Ad]]:
        tasks = [asyncio.create_task(self._fetch(target, side)) for side in ("BUY", "SELL")]
        try:
            buy, sell = await asyncio.gather(*tasks)
            return buy, sell
        finally:
            # Una pierna fallida no debe dejar otra consulta en segundo plano.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def process_revalidation(self, targets: list[WatchTarget]) -> None:
        """Una solicitud por turno, con la misma fuente y límite de concurrencia."""
        if self._revalidations is None:
            return
        request = None
        target = None
        checking = False
        try:
            request = self._revalidations.claim()
            if request is None:
                return
            payload = request["payload"]
            original = payload["target"]
            target = next((t for t in targets if t.asset == original["asset"] and t.fiat == original["fiat"]), None)
            current = asdict(target) if target else {}
            # JSON usa listas y textos; normalizar sin depender del adaptador.
            same = target is not None and set(current) == set(original) and all(
                (list(current[key]) == value if key == "pay_methods" else str(current[key]) == str(value))
                for key, value in original.items())
            if not same:
                self._revalidations.finish(request, {"message": "La configuración cambió; solicita una nueva comprobación."}, error=True)
                return
            checking = True
            buys, sells = await asyncio.wait_for(self._fetch_book(target), timeout=20)
            checking = False
            now = self._clock()
            if self._observer:
                self._observer.record(target, buys, sells, now)
            routes = evaluate_routes(buys, sells, target, funds=decimal_amount(payload["funds"]))
            route = next((r for r in routes if r.key == payload["route_key"]), None)
            alternative = next((r for r in routes if r.state == "compatible"
                                and r.buy_method == payload["buy_method"]
                                and r.sell_method == payload["sell_method"]), None)
            self._revalidations.finish(request, {
                "state": route.state if route else "not_observed", "route": asdict(route) if route else None,
                "alternative": asdict(alternative) if route is None and alternative else None,
                "checked_at": now.isoformat(), "target": asdict(target),
                "message": "Nueva observación del mercado; no reserva anuncios.",
            })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # No enviar excepciones ni datos financieros a los logs públicos.
            logger.warning("No se pudo procesar una comprobación de mercado.")
            try:
                if checking and target and self._observer:
                    self._observer.failed(target, self._clock())
                if request is not None:
                    message = ("La consulta del mercado tardó demasiado; inténtalo de nuevo."
                               if isinstance(exc, TimeoutError) else "No se pudo consultar el mercado; inténtalo de nuevo.")
                    self._revalidations.finish(request, {"message": message}, error=True)
            except Exception:
                logger.warning("No se pudo guardar el resultado de la comprobación.")
