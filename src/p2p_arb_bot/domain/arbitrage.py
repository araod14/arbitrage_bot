"""Lógica de cálculo de arbitraje. Funciones puras, sin I/O.

Mide el spread entre el mejor lado de compra y el mejor lado de venta del libro
P2P, filtrando por límites de monto y por método de pago, e incluyendo pares
cruzados (comprar con método A, vender con método B).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable

from .models import Ad, Opportunity, WatchTarget

# Etiqueta interna para el caso "sin filtrar por método" (target sin métodos).
_ALL = "ALL"


def amount_in_fiat(max_usdt: Decimal, ref_price: Decimal) -> Decimal:
    """Convierte un monto en USDT a fiat usando un precio de referencia."""
    return max_usdt * ref_price


def accepts_amount(ad: Ad, max_usdt: Decimal, ref_price: Decimal) -> bool:
    """¿El anuncio acepta operar ``max_usdt`` (convertido a fiat)?

    Comprueba que el monto en fiat caiga dentro de [min_amount, max_amount] del
    anuncio (límites inclusive).
    """
    amount = amount_in_fiat(max_usdt, ref_price)
    return ad.min_amount <= amount <= ad.max_amount


def _eligible(ads: Iterable[Ad], method: str, max_usdt: Decimal) -> list[Ad]:
    """Filtra anuncios por método de pago y por que acepten el monto.

    Cada anuncio se evalúa contra sus propios límites usando su propio precio
    como referencia (es el monto fiat que realmente se transaría con él).
    """
    out: list[Ad] = []
    for ad in ads:
        if method != _ALL and method not in ad.pay_methods:
            continue
        if accepts_amount(ad, max_usdt, ad.price):
            out.append(ad)
    return out


def best_buy(ads: Iterable[Ad], max_usdt: Decimal, method: str = _ALL) -> Ad | None:
    """Mejor anuncio para COMPRAR USDT: el de precio más bajo que acepte el monto."""
    eligible = _eligible(ads, method, max_usdt)
    if not eligible:
        return None
    return min(eligible, key=lambda a: a.price)


def best_sell(ads: Iterable[Ad], max_usdt: Decimal, method: str = _ALL) -> Ad | None:
    """Mejor anuncio para VENDER USDT: el de precio más alto que acepte el monto."""
    eligible = _eligible(ads, method, max_usdt)
    if not eligible:
        return None
    return max(eligible, key=lambda a: a.price)


def compute_spread(buy_price: Decimal, sell_price: Decimal) -> Decimal:
    """Spread porcentual: (sell - buy) / buy * 100."""
    if buy_price <= 0:
        return Decimal("0")
    return (sell_price - buy_price) / buy_price * Decimal("100")


def _methods_for(target: WatchTarget) -> list[str]:
    return list(target.pay_methods) if target.pay_methods else [_ALL]


def best_spread(
    buy_ads: Iterable[Ad],
    sell_ads: Iterable[Ad],
    target: WatchTarget,
) -> Decimal | None:
    """Mejor net_pct alcanzable (para heartbeats), sin aplicar el umbral.

    Devuelve ``None`` si no hay ningún par compra/venta elegible.
    """
    buy_ads = list(buy_ads)
    sell_ads = list(sell_ads)
    methods = _methods_for(target)
    best: Decimal | None = None
    for bm in methods:
        bb = best_buy(buy_ads, target.max_usdt, bm)
        if bb is None:
            continue
        for sm in methods:
            ss = best_sell(sell_ads, target.max_usdt, sm)
            if ss is None:
                continue
            net = compute_spread(bb.price, ss.price) - target.fee_buffer_pct
            if best is None or net > best:
                best = net
    return best


def find_best_opportunity(
    buy_ads: Iterable[Ad],
    sell_ads: Iterable[Ad],
    target: WatchTarget,
    now: datetime,
) -> Opportunity | None:
    """Devuelve la mejor oportunidad (incluyendo pares cruzados) si supera el umbral.

    Evalúa todas las combinaciones (método de compra A, método de venta B),
    elige la de mayor ``net_pct`` y la devuelve solo si
    ``net_pct >= target.threshold_pct``. Si no, devuelve ``None``.
    """
    buy_ads = list(buy_ads)
    sell_ads = list(sell_ads)
    methods = _methods_for(target)

    best_buy_ad: Ad | None = None
    best_sell_ad: Ad | None = None
    best_buy_method = ""
    best_sell_method = ""
    best_net: Decimal | None = None

    for bm in methods:
        bb = best_buy(buy_ads, target.max_usdt, bm)
        if bb is None:
            continue
        for sm in methods:
            ss = best_sell(sell_ads, target.max_usdt, sm)
            if ss is None:
                continue
            net = compute_spread(bb.price, ss.price) - target.fee_buffer_pct
            if best_net is None or net > best_net:
                best_net = net
                best_buy_ad, best_sell_ad = bb, ss
                best_buy_method, best_sell_method = bm, sm

    if best_net is None or best_buy_ad is None or best_sell_ad is None:
        return None
    if best_net < target.threshold_pct:
        return None

    spread = compute_spread(best_buy_ad.price, best_sell_ad.price)
    est_profit = (best_sell_ad.price - best_buy_ad.price) * target.max_usdt
    # Ganancia neta estimada en USDT: lo que ganarías sobre el monto operado,
    # después del buffer de fees (net_pct ya lo descuenta).
    est_profit_usdt = target.max_usdt * best_net / Decimal("100")

    return Opportunity(
        detected_at=now,
        fiat=target.fiat,
        asset=target.asset,
        buy_pay_method=best_buy_method,
        sell_pay_method=best_sell_method,
        buy_price=best_buy_ad.price,
        sell_price=best_sell_ad.price,
        spread_pct=spread,
        net_pct=best_net,
        max_usdt=target.max_usdt,
        buy_adv_no=best_buy_ad.adv_no,
        sell_adv_no=best_sell_ad.adv_no,
        buy_advertiser=best_buy_ad.advertiser_name,
        sell_advertiser=best_sell_ad.advertiser_name,
        buy_url=best_buy_ad.advertiser_url,
        sell_url=best_sell_ad.advertiser_url,
        est_profit_fiat=est_profit,
        est_profit_usdt=est_profit_usdt,
    )
