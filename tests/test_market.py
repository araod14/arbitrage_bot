"""Regresiones de dimensionamiento variable y capital compartido."""

from dataclasses import replace
from decimal import Decimal

import pytest

from p2p_arb_bot.domain.market import FundAccount, decimal_amount, evaluate_routes, funded_routes
from tests.test_monitor import make_ad, target

D = Decimal


def book():
    buy = replace(make_ad("b", "10", "BUY"), min_amount=D("100"), max_amount=D("1000"), pay_methods=("Banesco", "PagoMovil"))
    sell = replace(make_ad("s", "11", "SELL"), min_amount=D("110"), max_amount=D("1100"), pay_methods=("Mercantil",))
    return [buy], [sell]


def account(**kwargs):
    data = dict(id=1, name="Cuenta", fiat="VES", balance=D("500"), reserved=D("0"),
                methods=("Banesco", "PagoMovil"), can_receive=True, version=1, updated_at="2026-01-01")
    data.update(kwargs)
    return FundAccount(**data)


def test_dimensiona_compra_menor_que_fondo_global():
    buys, sells = book()
    routes = evaluate_routes(buys, sells, target(max_fiat=D("5000")), funds=D("500"))
    assert len(routes) == 2
    assert all(r.state == "compatible" and r.units == D("50") for r in routes)
    assert routes[0].amount_fiat == D("500")
    assert routes[0].profit_fiat == D("50")


def test_limites_venta_se_calculan_con_unidades_de_compra():
    buys, sells = book()
    sells = [replace(sells[0], min_amount=D("550"), max_amount=D("660"), surplus=D("55"))]
    route = evaluate_routes(buys, sells, target(max_fiat=D("1000")))[0]
    assert route.units == D("55")
    assert route.minimum_fiat == D("500")
    route = evaluate_routes(buys, sells, target(max_fiat=D("1000")), funds=D("499"))[0]
    assert route.state == "funds" and route.units == 0


def test_limites_incompatibles_no_se_presentan_como_saldo_insuficiente():
    buys, sells = book()
    sells = [replace(sells[0], min_amount=D("550"), surplus=D("40"))]
    assert evaluate_routes(buys, sells, target())[0].state == "limits"


def test_recibir_no_requiere_saldo_y_metodos_comparten_disponible():
    buys, sells = book()
    accounts = [account(reserved=D("200")),
                account(id=2, name="Receptor", balance=D("0"), methods=("Mercantil",))]
    rows = funded_routes(buys, sells, target(), accounts)
    assert len(rows) == 2
    assert all(reason == "compatible" and route.amount_fiat == D("300")
               for route, _, reason in rows)
    assert {a.id for _, a, _ in rows} == {1}


def test_no_suma_saldos_entre_cuentas():
    buys, sells = book()
    buys = [replace(buys[0], min_amount=D("600"))]
    accounts = [account(), account(id=2), account(id=3, balance=D("0"), methods=("Mercantil",))]
    assert all(reason == "funds" for _, _, reason in funded_routes(buys, sells, target(), accounts))


def test_falta_receptor_y_falta_configuracion_son_distintos():
    buys, sells = book()
    assert {reason for _, _, reason in funded_routes(buys, sells, target(), [account()])} == {"receiver"}
    assert {reason for _, _, reason in funded_routes(buys, sells, target(), [])} == {"unconfigured"}


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "texto", ""])
def test_importes_invalidos_no_se_normalizan_a_cero(value):
    with pytest.raises(ValueError):
        decimal_amount(value)


@pytest.mark.parametrize("field,value", [("price", "NaN"), ("price", "0"), ("surplus", "Infinity"), ("max_amount", "0")])
def test_descarta_anuncios_invalidos(field, value):
    buys, sells = book()
    assert evaluate_routes([replace(buys[0], **{field: D(value)})], sells, target()) == []


def test_metodos_siempre_concretos_y_precio_no_cambia_identidad():
    buys, sells = book()
    before = evaluate_routes(buys, sells, target())[0]
    after = evaluate_routes([replace(buys[0], price=D("9"))], sells, target())[0]
    assert before.key == after.key
    assert before.buy_method != "ALL" and before.sell_method != "ALL"


def test_redondeo_nunca_reserva_mas_que_el_disponible():
    buy = replace(make_ad("b", "14", "BUY"), min_amount=D("0"))
    sell = make_ad("s", "15", "SELL")
    route = evaluate_routes([buy], [sell], target(max_fiat=D("2")))[0]
    assert route.state == "compatible"
    assert route.amount_fiat <= D("2") and route.units == D("0.14285714")


def test_tope_por_operacion_distinto_de_saldo_bancario():
    buys, sells = book()
    buys = [replace(buys[0], min_amount=D("600"))]
    accounts = [account(balance=D("1000")), account(id=2, balance=D("0"), methods=("Mercantil",))]
    assert {reason for _, _, reason in funded_routes(buys, sells, target(max_fiat=D("500")), accounts)} == {"budget"}


def test_no_confirma_colision_de_identificadores_redondeados():
    buys, sells = book()
    routes = evaluate_routes(buys + [replace(buys[0], price=D("9"))], sells, target())
    assert len(routes) == 2
    assert all(r.state == "identity" and r.units == 0 for r in routes)


def test_anuncio_duplicado_identico_no_duplica_ruta():
    buys, sells = book()
    assert len(evaluate_routes(buys * 2, sells * 2, target())) == 2
