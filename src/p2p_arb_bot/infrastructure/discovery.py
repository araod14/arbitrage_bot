"""Descubrimiento de métodos de pago disponibles para un par/fiat.

Hace una consulta inicial al libro y extrae los métodos de pago distintos que
aparecen en los anuncios, para ofrecérselos al usuario en un menú numerado.
"""

from __future__ import annotations

from ..application.ports import MarketDataSource


async def discover_pay_methods(
    source: MarketDataSource,
    asset: str,
    fiat: str,
    *,
    rows: int = 20,
) -> list[tuple[str, str]]:
    """Devuelve pares ``(identifier, nombre_legible)`` distintos, ordenados.

    Consulta ambos lados (BUY y SELL) para cubrir más métodos. El nombre legible
    se reconstruye del identifier ya que el endpoint devuelve el identifier como
    fuente de verdad; si coinciden, basta el identifier.
    """
    seen: dict[str, str] = {}
    for trade_type in ("BUY", "SELL"):
        ads = await source.fetch_ads(
            asset=asset,
            fiat=fiat,
            trade_type=trade_type,  # type: ignore[arg-type]
            pay_types=[],
            rows=rows,
        )
        for ad in ads:
            for ident in ad.pay_methods:
                seen.setdefault(ident, ident)
    return sorted(seen.items(), key=lambda kv: kv[0].lower())
