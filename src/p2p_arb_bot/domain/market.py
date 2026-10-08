"""Evaluación pura de rutas y capital disponible, sin acceso a bancos ni red."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from hashlib import sha256
import json

from .arbitrage import compute_spread, drop_outliers
from .models import Ad, WatchTarget


@dataclass(frozen=True, slots=True)
class FundAccount:
    id: int
    name: str
    fiat: str
    balance: Decimal
    reserved: Decimal
    methods: tuple[str, ...]
    can_receive: bool
    version: int
    updated_at: str

    @property
    def available(self) -> Decimal:
        return self.balance - self.reserved


@dataclass(frozen=True, slots=True)
class Route:
    key: str
    buy: Ad
    sell: Ad
    buy_method: str
    sell_method: str
    net_pct: Decimal
    units: Decimal
    minimum_fiat: Decimal
    state: str

    @property
    def amount_fiat(self) -> Decimal:
        return self.units * self.buy.price

    @property
    def profit_fiat(self) -> Decimal:
        return self.amount_fiat * self.net_pct / Decimal("100")


def decimal_amount(value: object) -> Decimal:
    """Importe financiero explícito: nunca convierte entradas inválidas a cero."""
    try:
        result = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Introduce un importe numérico válido.") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("El importe debe ser finito y no negativo.")
    return result


def route_key(target: WatchTarget, buy: Ad, sell: Ad, bm: str, sm: str) -> str:
    """Identidad aproximada: contraparte, anuncio observado y métodos concretos.

    No certifica la identidad del anuncio: advNo puede llegar redondeado.
    Los precios se guardan como evidencia, no como parte de la agrupación.
    """
    identity = (target.asset, target.fiat, buy.advertiser_no, buy.adv_no,
                sell.advertiser_no, sell.adv_no, bm, sm)
    return sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def _valid(ad: Ad) -> bool:
    values = (ad.price, ad.surplus, ad.min_amount, ad.max_amount)
    return (all(v.is_finite() for v in values) and ad.price > 0
            and ad.surplus >= 0 and 0 <= ad.min_amount <= ad.max_amount
            and ad.max_amount > 0)


def evaluate_routes(
    buys: list[Ad], sells: list[Ad], target: WatchTarget,
    *, funds: Decimal | None = None,
) -> list[Route]:
    """Dimensiona cada pareja antes de filtrar: permite usar parte del fondo.

    Ambos inventarios se comparan con LAS MISMAS unidades. Cada límite fiat
    se convierte al precio de su lado. Los métodos siempre son concretos.
    """
    cap = target.max_fiat if funds is None else min(target.max_fiat, funds)
    if not cap.is_finite() or cap < 0:
        raise ValueError("El fondo debe ser finito y no negativo.")
    allowed = set(target.pay_methods)
    buy_methods = sorted({m for a in buys for m in a.pay_methods if not allowed or m in allowed})
    sell_methods = sorted({m for a in sells for m in a.pay_methods if not allowed or m in allowed})
    routes: list[Route] = []
    for bm in buy_methods:
        bb = drop_outliers([a for a in buys if _valid(a) and bm in a.pay_methods], target.outlier_max_dev_pct)
        for sm in sell_methods:
            ss = drop_outliers([a for a in sells if _valid(a) and sm in a.pay_methods], target.outlier_max_dev_pct)
            for buy in bb:
                for sell in ss:
                    floor = max(buy.min_amount / buy.price, sell.min_amount / sell.price)
                    ceiling = min(buy.max_amount / buy.price, sell.max_amount / sell.price,
                                  buy.surplus, sell.surplus)
                    units = min(cap / buy.price, ceiling)
                    # Coincide con las ocho cifras mostradas para cantidades cripto.
                    # Redondear hacia abajo evita reservar más fiat por redondeo.
                    with localcontext() as ctx:
                        ctx.prec = max(28, units.adjusted() + 12)
                        units = units.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                    net = compute_spread(buy.price, sell.price) - target.fee_buffer_pct
                    if ceiling < floor or ceiling <= 0:
                        state = "limits"
                    elif units < floor or units <= 0:
                        state = "funds"
                    elif sell.price <= buy.price or net < target.threshold_pct:
                        state = "margin"
                    else:
                        state = "compatible"
                    if not (buy.advertiser_no or buy.adv_no) or not (sell.advertiser_no or sell.adv_no):
                        state = "identity"
                    routes.append(Route(route_key(target, buy, sell, bm, sm), buy, sell,
                                        bm, sm, net, units if state == "compatible" else Decimal("0"),
                                        floor * buy.price, state))
    unique: dict[str, Route] = {}
    for route in routes:
        previous = unique.get(route.key)
        if previous and (previous.buy != route.buy or previous.sell != route.sell):
            # advNo redondeados pueden colisionar: no escoger uno silenciosamente.
            unique[route.key] = replace(previous, state="identity", units=Decimal("0"))
        elif previous is None:
            unique[route.key] = route
    return sorted(unique.values(), key=lambda r: (r.state == "compatible", r.profit_fiat, r.net_pct), reverse=True)


def funded_routes(
    buys: list[Ad], sells: list[Ad], target: WatchTarget, accounts: list[FundAccount],
) -> list[tuple[Route, FundAccount | None, str]]:
    """Evalúa cuentas por separado: alternativas que compiten por el mismo saldo."""
    matching = [a for a in accounts if a.fiat == target.fiat]
    receivers = {m for a in matching if a.can_receive for m in a.methods}
    results: list[tuple[Route, FundAccount | None, str]] = []
    if not matching:
        return [(r, None, "unconfigured") for r in evaluate_routes(buys, sells, target)]
    seen: set[str] = set()
    for account in matching:
        for route in evaluate_routes(buys, sells, target, funds=account.available):
            if route.buy_method not in account.methods:
                continue
            seen.add(route.key)
            reason = route.state if route.sell_method in receivers else "receiver"
            if reason == "funds" and account.available >= route.minimum_fiat > target.max_fiat:
                reason = "budget"
            results.append((route, account, reason))
    # Las rutas sin cuenta de compra configurada también explican la incompatibilidad.
    results.extend((r, None, "unconfigured") for r in evaluate_routes(buys, sells, target) if r.key not in seen)
    return sorted(results, key=lambda item: (item[2] == "compatible", item[0].profit_fiat,
                                           item[0].net_pct), reverse=True)
