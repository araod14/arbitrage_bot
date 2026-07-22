"""Lógica de cálculo de arbitraje. Funciones puras, sin I/O.

Mide el spread entre el mejor lado de compra y el mejor lado de venta del libro
P2P, filtrando por límites de monto y por método de pago, e incluyendo pares
cruzados (comprar con método A, vender con método B).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable

from .models import Ad, Opportunity, RealizedPnl, TradeSizing, WatchTarget, book_url

# Etiqueta interna para el caso "sin filtrar por método" (target sin métodos).
_ALL = "ALL"


def suggested_trade(
    max_usdt: Decimal,
    buy_price: Decimal,
    sell_price: Decimal,
    buy_min: Decimal,
    buy_max: Decimal,
    sell_min: Decimal,
    sell_max: Decimal,
) -> TradeSizing:
    """Cuánto operar (en unidades del asset y en fiat) acotado por el fondo y los
    límites de anuncio.

    ``max_usdt`` son las unidades del asset que cubre el fondo a ese precio de
    compra (lo que guarda ``Opportunity.max_usdt``), no el fondo en fiat.

    - Piso: ``max`` de los mínimos de compra/venta, cada uno convertido a unidades
      con su propio precio (``min_amount`` está en fiat).
    - Techo: el fondo ``max_usdt``, recortado por los máximos de compra/venta (un
      máximo ``<= 0`` significa "sin dato" y no acota).
    - Si el piso supera al techo, no es factible: ``usable`` queda a 0.

    Precios ``<= 0`` (datos ausentes/erróneos) se ignoran con seguridad: ese lado no
    aporta ni piso ni techo. Función pura, sin I/O.
    """

    def _to_usdt(fiat: Decimal, price: Decimal) -> Decimal | None:
        if price <= 0 or fiat <= 0:
            return None
        return fiat / price

    lows = [v for v in (_to_usdt(buy_min, buy_price), _to_usdt(sell_min, sell_price)) if v is not None]
    min_required_usdt = max(lows) if lows else Decimal("0")

    usable_usdt = max_usdt
    for cap in (_to_usdt(buy_max, buy_price), _to_usdt(sell_max, sell_price)):
        if cap is not None:
            usable_usdt = min(usable_usdt, cap)

    feasible = usable_usdt >= min_required_usdt
    if not feasible:
        usable_usdt = Decimal("0")

    return TradeSizing(
        usable_usdt=usable_usdt,
        usable_fiat_buy=usable_usdt * buy_price,
        usable_fiat_sell=usable_usdt * sell_price,
        min_required_usdt=min_required_usdt,
        min_required_fiat_buy=min_required_usdt * buy_price,
        feasible=feasible,
    )


def realized_pnl(fiat_in: Decimal, fiat_out: Decimal, usdt: Decimal) -> RealizedPnl:
    """Ganancia REAL de una operación cerrada, a partir de lo que movió el banco.

    Se calcula sobre los montos fiat efectivos (``fiat_in`` pagado al comprar,
    ``fiat_out`` recibido al vender) y no sobre los precios: así las comisiones y
    los redondeos que se comieron parte del spread quedan reflejados, que es
    justo lo que interesa medir contra la estimación del bot.

    ``fiat_in <= 0`` (dato ausente o erróneo) devuelve ceros en vez de dividir por
    cero: no se puede calcular un % sobre una inversión desconocida. Función pura.
    """
    if fiat_in <= 0:
        return RealizedPnl(
            profit_fiat=Decimal("0"), profit_usdt=Decimal("0"), net_pct=Decimal("0")
        )

    profit_fiat = fiat_out - fiat_in
    return RealizedPnl(
        profit_fiat=profit_fiat,
        # usdt/fiat_in es el precio de compra realmente pagado; convierte la
        # ganancia fiat a USDT sin depender del precio nominal del anuncio.
        profit_usdt=profit_fiat * usdt / fiat_in if usdt > 0 else Decimal("0"),
        net_pct=profit_fiat / fiat_in * Decimal("100"),
    )


def accepts_amount(ad: Ad, max_fiat: Decimal) -> bool:
    """¿El anuncio acepta operar el fondo ``max_fiat``?

    Los límites del anuncio ya están en fiat, igual que el fondo, así que la
    comparación es directa contra [min_amount, max_amount] (límites inclusive).
    """
    return ad.min_amount <= max_fiat <= ad.max_amount


def has_inventory(ad: Ad, max_fiat: Decimal) -> bool:
    """¿El anuncio tiene inventario para absorber el fondo ``max_fiat``?

    Comprueba ``surplusAmount`` (inventario real del anuncio, en unidades del
    asset) contra las unidades que compraría el fondo a este precio. Muchos
    anuncios "cebo" muestran un precio muy bueno con poquísima disponibilidad: sin
    este filtro serían elegidos como mejor compra/venta aunque no puedas llenarlos.

    Un precio ``<= 0`` (dato ausente/erróneo) descarta el anuncio: no se puede
    saber cuántas unidades cubre el fondo.
    """
    if ad.price <= 0:
        return False
    return ad.surplus >= max_fiat / ad.price


def _median_price(ads: list[Ad]) -> Decimal:
    """Mediana de los precios de una lista no vacía de anuncios."""
    prices = sorted(a.price for a in ads)
    n = len(prices)
    mid = n // 2
    if n % 2 == 1:
        return prices[mid]
    return (prices[mid - 1] + prices[mid]) / Decimal("2")


def drop_outliers(ads: list[Ad], max_dev_pct: Decimal) -> list[Ad]:
    """Descarta anuncios cuyo precio se desvía más de ``max_dev_pct`` de la mediana.

    Protege contra anuncios "cebo": precios absurdos muy por encima (en venta) o
    por debajo (en compra) del mercado que, sin este filtro, serían elegidos como
    mejor compra/venta e inflarían el spread con oportunidades falsas.

    ``max_dev_pct <= 0`` desactiva el filtro. Con menos de 3 anuncios no se filtra
    (la mediana no es fiable para distinguir el outlier). Si el filtro dejara la
    lista vacía, se devuelve la original sin tocar (defensa ante configs extremas).
    """
    if max_dev_pct <= 0 or len(ads) < 3:
        return ads
    median = _median_price(ads)
    if median <= 0:
        return ads
    tol = median * max_dev_pct / Decimal("100")
    lo, hi = median - tol, median + tol
    kept = [a for a in ads if lo <= a.price <= hi]
    return kept or ads


def _eligible(
    ads: Iterable[Ad],
    method: str,
    max_fiat: Decimal,
    max_dev_pct: Decimal = Decimal("0"),
) -> list[Ad]:
    """Filtra por método de pago, por que acepten el monto, por inventario y outliers.

    Cada anuncio se evalúa contra sus propios límites (en fiat, igual que el
    fondo) y contra su disponibilidad real (``surplusAmount``, en unidades del
    asset). Tras ese filtro se descartan los precios atípicos respecto a la
    mediana del libro.
    """
    out: list[Ad] = []
    for ad in ads:
        if method != _ALL and method not in ad.pay_methods:
            continue
        if accepts_amount(ad, max_fiat) and has_inventory(ad, max_fiat):
            out.append(ad)
    return drop_outliers(out, max_dev_pct)


def best_buy(
    ads: Iterable[Ad],
    max_fiat: Decimal,
    method: str = _ALL,
    max_dev_pct: Decimal = Decimal("0"),
) -> Ad | None:
    """Mejor anuncio para COMPRAR el asset: el de precio más bajo que acepte el monto."""
    eligible = _eligible(ads, method, max_fiat, max_dev_pct)
    if not eligible:
        return None
    return min(eligible, key=lambda a: a.price)


def best_sell(
    ads: Iterable[Ad],
    max_fiat: Decimal,
    method: str = _ALL,
    max_dev_pct: Decimal = Decimal("0"),
) -> Ad | None:
    """Mejor anuncio para VENDER el asset: el de precio más alto que acepte el monto."""
    eligible = _eligible(ads, method, max_fiat, max_dev_pct)
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
    dev = target.outlier_max_dev_pct
    best: Decimal | None = None
    for bm in methods:
        bb = best_buy(buy_ads, target.max_fiat, bm, dev)
        if bb is None:
            continue
        for sm in methods:
            ss = best_sell(sell_ads, target.max_fiat, sm, dev)
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
    dev = target.outlier_max_dev_pct

    best_buy_ad: Ad | None = None
    best_sell_ad: Ad | None = None
    best_buy_method = ""
    best_sell_method = ""
    best_net: Decimal | None = None

    for bm in methods:
        bb = best_buy(buy_ads, target.max_fiat, bm, dev)
        if bb is None:
            continue
        for sm in methods:
            ss = best_sell(sell_ads, target.max_fiat, sm, dev)
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
    # Unidades del asset que compra el fondo a este precio: es lo que hace que un
    # mismo MAX_FIAT sirva para USDT y para BTC. best_buy_ad.price > 0 lo garantiza
    # has_inventory, que descarta los precios no positivos.
    units = target.max_fiat / best_buy_ad.price
    est_profit = (best_sell_ad.price - best_buy_ad.price) * units
    # Ganancia neta estimada en unidades del asset: lo que ganarías sobre el monto
    # operado, después del buffer de fees (net_pct ya lo descuenta).
    est_profit_usdt = units * best_net / Decimal("100")

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
        max_usdt=units,
        buy_adv_no=best_buy_ad.adv_no,
        sell_adv_no=best_sell_ad.adv_no,
        buy_advertiser=best_buy_ad.advertiser_name,
        sell_advertiser=best_sell_ad.advertiser_name,
        # Enlace al libro en vivo de cada lado/método (el centinela _ALL => todos).
        buy_url=book_url(
            trade_type="BUY",
            asset=target.asset,
            fiat=target.fiat,
            pay_method="" if best_buy_method == _ALL else best_buy_method,
        ),
        sell_url=book_url(
            trade_type="SELL",
            asset=target.asset,
            fiat=target.fiat,
            pay_method="" if best_sell_method == _ALL else best_sell_method,
        ),
        est_profit_fiat=est_profit,
        est_profit_usdt=est_profit_usdt,
        buy_min_amount=best_buy_ad.min_amount,
        buy_max_amount=best_buy_ad.max_amount,
        sell_min_amount=best_sell_ad.min_amount,
        sell_max_amount=best_sell_ad.max_amount,
    )
