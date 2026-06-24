"""Puertos (interfaces) de la capa de aplicación.

Definidos con ``typing.Protocol`` para permitir inversión de dependencias: el
caso de uso depende de estas abstracciones, no de implementaciones concretas.
Cualquier objeto que cumpla la firma sirve (duck typing estructural), lo que
hace triviales los fakes en tests.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from ..domain.models import Ad, Opportunity, TradeType, WatchTarget


@runtime_checkable
class MarketDataSource(Protocol):
    """Fuente de datos de mercado (un exchange/par/fiat). Hoy Binance P2P."""

    async def fetch_ads(
        self,
        *,
        asset: str,
        fiat: str,
        trade_type: TradeType,
        pay_types: list[str],
        trans_amount: str = "",
        merchant_check: bool = False,
        rows: int = 20,
    ) -> list[Ad]:
        """Devuelve la lista de anuncios del lado ``trade_type``.

        Implementaciones deben validar la respuesta (no solo el status) y
        lanzar/propagar errores si los datos no son utilizables.
        """
        ...

    async def aclose(self) -> None:
        """Libera recursos (conexiones). Idempotente."""
        ...


@runtime_checkable
class OpportunityRepository(Protocol):
    """Almacenamiento de oportunidades. Hoy SQLite; mañana Postgres/CSV."""

    def save(self, opp: Opportunity) -> None: ...

    def recent_duplicate(self, opp: Opportunity, window_s: int) -> bool:
        """¿Ya se registró una oportunidad idéntica en los últimos ``window_s`` s?"""
        ...

    def close(self) -> None: ...


@runtime_checkable
class Notifier(Protocol):
    """Canal de aviso. Hoy consola; mañana Telegram/email/webhook."""

    async def notify_opportunity(self, opp: Opportunity) -> None: ...

    async def notify_heartbeat(
        self, target: WatchTarget, best_spread: Decimal | None
    ) -> None: ...
