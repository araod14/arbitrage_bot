"""Adaptador ``MarketDataSource`` para el endpoint público de Binance P2P.

Usa ``curl_cffi`` con impersonation TLS porque el endpoint puede hacer
fingerprinting TLS/JA3: ``requests``/``httpx`` pueden bloquearse aunque envíen
un User-Agent correcto. ``curl_cffi`` con ``impersonate="chrome"`` replica el
handshake de Chrome a nivel de TLS/HTTP2 sin necesidad de un navegador real
(es un endpoint JSON, no hace falta ejecutar JS).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from decimal import Decimal, InvalidOperation
from typing import Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError

from ..domain.models import Ad, TradeType

logger = logging.getLogger(__name__)

_ENDPOINT = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

# Headers mínimos. NO fijamos User-Agent: `impersonate` ya pone uno coherente
# con el resto del fingerprint; sobrescribirlo rompería la coherencia.
_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://p2p.binance.com",
    "Referer": "https://p2p.binance.com/",
}


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


class BinanceP2PSource:
    """Implementación concreta de ``MarketDataSource`` sobre Binance P2P."""

    def __init__(
        self,
        *,
        impersonate: str = "chrome",
        timeout_s: int = 30,
        max_attempts: int = 5,
        proxy: str | None = None,
    ) -> None:
        self._impersonate = impersonate
        self._timeout_s = timeout_s
        self._max_attempts = max_attempts
        # El proxy se toma de un argumento o de variables de entorno; nunca se
        # hardcodean credenciales en el código.
        self._proxy = proxy or os.getenv("PROXY") or os.getenv("HTTPS_PROXY") or None

    def _new_session(self) -> AsyncSession:
        # Una sesión por petición: lanzar varias transferencias concurrentes
        # sobre una AsyncSession compartida puede bloquearse (deadlock) según el
        # build de libcurl. Con sesión por request el paralelismo (gather de
        # varios lados/métodos) es seguro; el coste es un handshake TLS extra,
        # despreciable al ritmo de polling de este bot.
        proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
        return AsyncSession(
            impersonate=self._impersonate,
            headers=_HEADERS,
            proxies=proxies,
        )

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
        payload = {
            "asset": asset,
            "fiat": fiat,
            "page": 1,
            "rows": rows,
            "tradeType": trade_type,
            "payTypes": pay_types,
            "transAmount": trans_amount,
            "merchantCheck": merchant_check,
        }
        data = await self._post_with_retry(payload)
        return [self._parse_ad(item, trade_type) for item in data]

    async def _post_with_retry(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """POST con backoff exponencial + jitter ante 403/429/timeout/success=false.

        Nada de sleeps fijos: cada reintento espera ``2**intento + random``.
        """
        last_error: str = "desconocido"

        async with self._new_session() as session:
            for attempt in range(self._max_attempts):
                try:
                    resp = await session.post(
                        _ENDPOINT, json=payload, timeout=self._timeout_s
                    )
                except RequestsError as exc:
                    last_error = f"error de red: {exc}"
                    await self._backoff(attempt)
                    continue

                if resp.status_code in (403, 429, 503):
                    last_error = f"status {resp.status_code}"
                    # Respeta Retry-After si viene; si no, backoff exponencial.
                    retry_after = resp.headers.get("Retry-After")
                    await self._backoff(attempt, retry_after)
                    continue

                if resp.status_code != 200:
                    last_error = f"status {resp.status_code}"
                    await self._backoff(attempt)
                    continue

                # Un 200 no basta: validar success=true y data antes de procesar.
                try:
                    body = resp.json()
                except ValueError:
                    last_error = "cuerpo no es JSON"
                    await self._backoff(attempt)
                    continue

                if not body.get("success"):
                    last_error = f"success=false ({body.get('code')})"
                    await self._backoff(attempt)
                    continue

                data = body.get("data") or []
                # `data` vacío puede ser legítimo (no hay anuncios), no un error.
                return data

        raise RuntimeError(
            f"Binance P2P no respondió correctamente tras "
            f"{self._max_attempts} intentos ({last_error})"
        )

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = 2**attempt + random.random()
        else:
            wait = 2**attempt + random.random()
        logger.debug("Backoff %.2fs (intento %d)", wait, attempt + 1)
        await asyncio.sleep(wait)

    @staticmethod
    def _parse_ad(item: dict[str, Any], trade_type: TradeType) -> Ad:
        adv = item.get("adv", {})
        advertiser = item.get("advertiser", {})
        methods = tuple(
            m.get("identifier", "")
            for m in adv.get("tradeMethods", [])
            if m.get("identifier")
        )
        return Ad(
            adv_no=str(adv.get("advNo", "")),
            price=_to_decimal(adv.get("price")),
            surplus=_to_decimal(adv.get("surplusAmount")),
            min_amount=_to_decimal(adv.get("minSingleTransAmount")),
            max_amount=_to_decimal(adv.get("maxSingleTransAmount")),
            pay_methods=methods,
            advertiser_no=str(advertiser.get("userNo", "")),
            advertiser_name=str(advertiser.get("nickName", "")),
            trade_type=trade_type,
        )

    async def aclose(self) -> None:
        # Las sesiones se abren y cierran por petición (context manager), así que
        # no queda ningún recurso persistente que liberar. Se mantiene por el
        # contrato del puerto MarketDataSource y para futuros recursos.
        return None
