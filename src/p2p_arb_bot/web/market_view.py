"""Presentación privada de capacidad financiera sobre snapshots de mercado."""

from decimal import Decimal

from ..domain.market import funded_routes
from ..domain.models import book_url
from ..infrastructure.market_codec import target_dict
from . import market_reader

LABELS = {
    "compatible": "Compatible", "funds": "Saldo insuficiente", "limits": "Límites incompatibles",
    "margin": "Dejó de cumplir el margen", "receiver": "Método receptor no habilitado",
    "unconfigured": "Fondos sin configurar", "not_observed": "No observada en las filas consultadas",
    "budget": "Tope por operación insuficiente",
    "identity": "Identidad de ruta insuficiente o ambigua",
}


def recommendations(books: list[dict], accounts: list, revision: int,
                    requests: list[dict], freshness_s: int, limit: int = 50) -> list[dict]:
    rows = []
    for book in books:
        target = book["target"]
        for route, account, reason in funded_routes(book["buys"], book["sells"], target, accounts):
            request = next((r for r in requests if r["payload"]["route_key"] == route.key
                            and r["payload"].get("account_id", 0) == (account.id if account else 0)), None)
            checked = request and request["state"] == "completed" and request["result"]
            confirmed = bool(checked and checked.get("state") == "compatible"
                             and checked.get("route", {}).get("key") == route.key
                             and market_reader.age_seconds(checked["checked_at"]) < freshness_s
                             and checked["target"] == target_dict(target)
                             and checked["route"]["buy"]["price"] == str(route.buy.price)
                             and checked["route"]["sell"]["price"] == str(route.sell.price)
                             and checked["route"]["units"] == str(route.units)
                             and checked["route"]["net_pct"] == str(route.net_pct)
                             and request["payload"].get("funds_revision") == revision
                             and reason == "compatible" and not book["stale"])
            rows.append({"route": route, "target": target, "account": account,
                         "reason": reason, "label": "Mercado desactualizado" if book["stale"] else LABELS[reason],
                         "stale": book["stale"], "age_s": book["age_s"], "error": book["error"],
                         "revalidated": confirmed, "request": request,
                         "deficit": max(Decimal("0"), route.minimum_fiat - min(target.max_fiat, account.available)) if account else None,
                         "buy_url": book_url(trade_type="BUY", asset=target.asset, fiat=target.fiat, pay_method=route.buy_method),
                         "sell_url": book_url(trade_type="SELL", asset=target.asset, fiat=target.fiat, pay_method=route.sell_method)})
    rows.sort(key=lambda row: (not row["stale"] and row["reason"] == "compatible",
                               row["route"].profit_fiat, row["route"].net_pct), reverse=True)
    return rows[:limit]
