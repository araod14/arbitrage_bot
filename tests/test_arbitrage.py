"""Tests de la capa de dominio: cálculo de spread, filtrado y pares cruzados.

Sin red ni disco. Solo dataclasses y funciones puras.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from p2p_arb_bot.domain.arbitrage import (
    accepts_amount,
    best_buy,
    best_sell,
    best_spread,
    compute_spread,
    drop_outliers,
    find_best_opportunity,
    has_inventory,
    realized_pnl,
    suggested_trade,
)
from p2p_arb_bot.domain.models import Ad, WatchTarget

NOW = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
D = Decimal


def make_ad(
    *,
    adv_no: str,
    price: str,
    trade_type: str,
    methods: tuple[str, ...] = ("PagoMovil",),
    min_amount: str = "0",
    max_amount: str = "1000000",
    surplus: str = "10000",
) -> Ad:
    return Ad(
        adv_no=adv_no,
        price=D(price),
        surplus=D(surplus),
        min_amount=D(min_amount),
        max_amount=D(max_amount),
        pay_methods=methods,
        advertiser_no=f"user-{adv_no}",
        advertiser_name=f"name-{adv_no}",
        trade_type=trade_type,  # type: ignore[arg-type]
    )


def target(**kw) -> WatchTarget:
    base = dict(
        asset="USDT",
        fiat="VES",
        pay_methods=(),
        max_fiat=D("80000"),   # equivale a 100 USDT a 800 VES
        threshold_pct=D("1.0"),
        fee_buffer_pct=D("0"),
    )
    base.update(kw)
    return WatchTarget(**base)  # type: ignore[arg-type]


# --- compute_spread ----------------------------------------------------------

def test_compute_spread_basic():
    assert compute_spread(D("100"), D("110")) == D("10")


def test_compute_spread_negative_when_sell_below_buy():
    assert compute_spread(D("100"), D("95")) == D("-5")


def test_compute_spread_zero_buy_price_is_safe():
    assert compute_spread(D("0"), D("100")) == D("0")


# --- best_buy / best_sell ----------------------------------------------------

def test_best_buy_picks_lowest_price():
    ads = [
        make_ad(adv_no="a", price="800", trade_type="BUY"),
        make_ad(adv_no="b", price="795", trade_type="BUY"),
        make_ad(adv_no="c", price="810", trade_type="BUY"),
    ]
    assert best_buy(ads, D("80000")).adv_no == "b"


def test_best_sell_picks_highest_price():
    ads = [
        make_ad(adv_no="a", price="800", trade_type="SELL"),
        make_ad(adv_no="b", price="820", trade_type="SELL"),
        make_ad(adv_no="c", price="810", trade_type="SELL"),
    ]
    assert best_sell(ads, D("80000")).adv_no == "b"


def test_best_buy_returns_none_when_empty():
    assert best_buy([], D("80000")) is None


# --- accepts_amount / filtrado por límites -----------------------------------

def test_accepts_amount_within_limits():
    ad = make_ad(adv_no="a", price="800", trade_type="BUY",
                 min_amount="10000", max_amount="100000")
    # El fondo ya está en fiat: 80_000 VES cae dentro de [10_000, 100_000]
    assert accepts_amount(ad, D("80000")) is True


def test_accepts_amount_below_min_rejected():
    ad = make_ad(adv_no="a", price="800", trade_type="BUY",
                 min_amount="200000", max_amount="500000")
    # 80_000 < 200_000 -> rechazado
    assert accepts_amount(ad, D("80000")) is False


def test_accepts_amount_above_max_rejected():
    ad = make_ad(adv_no="a", price="800", trade_type="BUY",
                 min_amount="0", max_amount="50000")
    # 80_000 > 50_000 -> rechazado
    assert accepts_amount(ad, D("80000")) is False


def test_accepts_amount_boundaries_inclusive():
    ad = make_ad(adv_no="a", price="800", trade_type="BUY",
                 min_amount="80000", max_amount="80000")
    assert accepts_amount(ad, D("80000")) is True


def test_has_inventory_compares_unidades_no_fiat():
    # surplus está en unidades del asset; el fondo, en fiat. 800.000 VES a 800
    # son justo las 1.000 unidades del inventario.
    ad = make_ad(adv_no="a", price="800", trade_type="BUY", surplus="1000")
    assert has_inventory(ad, D("800000")) is True   # límite inclusive
    assert has_inventory(ad, D("800800")) is False  # 1001 unidades, no alcanza


def test_has_inventory_rejects_zero_price():
    # Sin precio no se sabe cuántas unidades compra el fondo: se descarta.
    ad = make_ad(adv_no="a", price="0", trade_type="BUY", surplus="10000")
    assert has_inventory(ad, D("80000")) is False


def test_best_buy_skips_ads_without_inventory():
    # Precio buenísimo (más bajo) pero sin inventario para el monto -> se descarta;
    # gana el siguiente con inventario suficiente. Simula un anuncio "cebo".
    ads = [
        make_ad(adv_no="cebo", price="790", trade_type="BUY", surplus="30"),
        make_ad(adv_no="real", price="800", trade_type="BUY", surplus="5000"),
    ]
    best = best_buy(ads, D("800000"))
    assert best is not None
    assert best.adv_no == "real"


def test_best_sell_skips_ads_without_inventory():
    ads = [
        make_ad(adv_no="cebo", price="830", trade_type="SELL", surplus="30"),
        make_ad(adv_no="real", price="820", trade_type="SELL", surplus="5000"),
    ]
    best = best_sell(ads, D("800000"))
    assert best is not None
    assert best.adv_no == "real"


def test_best_buy_skips_ads_outside_limits():
    ads = [
        # Precio más bajo pero su max no admite el monto -> se descarta.
        make_ad(adv_no="cheap", price="790", trade_type="BUY",
                min_amount="0", max_amount="1000"),
        make_ad(adv_no="ok", price="800", trade_type="BUY",
                min_amount="0", max_amount="1000000"),
    ]
    assert best_buy(ads, D("80000")).adv_no == "ok"


# --- find_best_opportunity ---------------------------------------------------

def test_opportunity_detected_above_threshold():
    buy_ads = [make_ad(adv_no="b1", price="800", trade_type="BUY")]
    sell_ads = [make_ad(adv_no="s1", price="820", trade_type="SELL")]
    opp = find_best_opportunity(buy_ads, sell_ads, target(threshold_pct=D("1.0")), NOW)
    assert opp is not None
    assert opp.buy_price == D("800")
    assert opp.sell_price == D("820")
    # spread = 20/800*100 = 2.5%
    assert opp.spread_pct == D("2.5")
    assert opp.net_pct == D("2.5")


def test_opportunity_carries_ad_limits():
    buy_ads = [make_ad(adv_no="b1", price="800", trade_type="BUY",
                       min_amount="50000", max_amount="900000")]
    sell_ads = [make_ad(adv_no="s1", price="820", trade_type="SELL",
                        min_amount="10000", max_amount="800000")]
    opp = find_best_opportunity(buy_ads, sell_ads, target(threshold_pct=D("1.0")), NOW)
    assert opp is not None
    assert opp.buy_min_amount == D("50000")
    assert opp.buy_max_amount == D("900000")
    assert opp.sell_min_amount == D("10000")
    assert opp.sell_max_amount == D("800000")


# --- suggested_trade ---------------------------------------------------------

def _sizing(**kw):
    base = dict(
        max_usdt=D("100"), buy_price=D("800"), sell_price=D("820"),
        buy_min=D("0"), buy_max=D("0"), sell_min=D("0"), sell_max=D("0"),
    )
    base.update(kw)
    return suggested_trade(**base)  # type: ignore[arg-type]


def test_suggested_trade_uses_full_fund_when_within_limits():
    # Fondo 100 USDT * 800 = 80.000 VES de compra; mínimos por debajo, sin tope.
    s = _sizing(buy_min=D("50000"), sell_min=D("10000"))
    assert s.feasible is True
    assert s.usable_usdt == D("100")
    assert s.usable_fiat_buy == D("80000")   # 100 * 800
    assert s.usable_fiat_sell == D("82000")  # 100 * 820


def test_suggested_trade_infeasible_when_fund_below_minimum():
    # Mínimo de compra 200.000 VES -> requiere 250 USDT, pero el fondo es 100.
    s = _sizing(buy_min=D("200000"))
    assert s.feasible is False
    assert s.usable_usdt == D("0")
    assert s.min_required_usdt == D("250")          # 200000 / 800
    assert s.min_required_fiat_buy == D("200000")   # 250 * 800


def test_suggested_trade_capped_by_ad_maximum():
    # Máximo de venta 41.000 VES -> 50 USDT; recorta el fondo de 100.
    s = _sizing(sell_max=D("41000"))
    assert s.feasible is True
    assert s.usable_usdt == D("50")                 # 41000 / 820
    assert s.usable_fiat_buy == D("40000")          # 50 * 800


def test_suggested_trade_safe_with_zero_price():
    # Precio 0 (dato ausente) no debe romper ni acotar por ese lado.
    s = _sizing(buy_price=D("0"), buy_min=D("50000"))
    assert s.usable_usdt == D("100")  # el fondo no se recorta
    assert s.feasible is True


def test_est_profit_usdt_from_net_pct():
    buy_ads = [make_ad(adv_no="b1", price="800", trade_type="BUY")]
    sell_ads = [make_ad(adv_no="s1", price="816", trade_type="SELL")]  # 2% bruto
    # buffer 0.5% -> net 1.5%; sobre 200 USDT -> 3 USDT estimados.
    t = target(max_fiat=D("160000"), threshold_pct=D("1.0"), fee_buffer_pct=D("0.5"))
    opp = find_best_opportunity(buy_ads, sell_ads, t, NOW)
    assert opp is not None
    assert opp.net_pct == D("1.5")
    assert opp.est_profit_usdt == D("3.0")


def test_unidades_fraccionarias_en_asset_caro():
    """Con una cripto cara el mismo fondo fiat compra una fracción de unidad.

    Es lo que hace que MAX_FIAT sirva igual para USDT que para BTC: el bot no
    guarda el fondo, guarda las unidades que ese fondo compra a ese precio.
    """
    # Precio tipo BTC/VES; el fondo son 10.000.000 VES.
    buy_ads = [make_ad(adv_no="b1", price="4000000000", trade_type="BUY",
                       surplus="5", max_amount="99000000")]
    sell_ads = [make_ad(adv_no="s1", price="4080000000", trade_type="SELL",
                        surplus="5", max_amount="99000000")]
    t = target(asset="BTC", max_fiat=D("10000000"), threshold_pct=D("1.0"))
    opp = find_best_opportunity(buy_ads, sell_ads, t, NOW)
    assert opp is not None
    # 10.000.000 / 4.000.000.000 = 0.0025 BTC
    assert opp.max_usdt == D("0.0025")
    assert opp.net_pct == D("2")
    # 0.0025 BTC * 2% = 0.00005 BTC
    assert opp.est_profit_usdt == D("0.00005")
    assert "/trade/buy/BTC?fiat=VES" in opp.buy_url


def test_no_opportunity_below_threshold():
    buy_ads = [make_ad(adv_no="b1", price="800", trade_type="BUY")]
    sell_ads = [make_ad(adv_no="s1", price="804", trade_type="SELL")]  # 0.5%
    opp = find_best_opportunity(buy_ads, sell_ads, target(threshold_pct=D("1.0")), NOW)
    assert opp is None


def test_fee_buffer_reduces_net_below_threshold():
    buy_ads = [make_ad(adv_no="b1", price="800", trade_type="BUY")]
    sell_ads = [make_ad(adv_no="s1", price="812", trade_type="SELL")]  # 1.5% bruto
    # buffer 1% -> net 0.5% < umbral 1% -> sin oportunidad
    t = target(threshold_pct=D("1.0"), fee_buffer_pct=D("1.0"))
    assert find_best_opportunity(buy_ads, sell_ads, t, NOW) is None


def test_cross_pair_selects_best_combination():
    # Comprar barato con método A, vender caro con método B.
    buy_ads = [
        make_ad(adv_no="bA", price="790", trade_type="BUY", methods=("A",)),
        make_ad(adv_no="bB", price="800", trade_type="BUY", methods=("B",)),
    ]
    sell_ads = [
        make_ad(adv_no="sA", price="805", trade_type="SELL", methods=("A",)),
        make_ad(adv_no="sB", price="825", trade_type="SELL", methods=("B",)),
    ]
    t = target(pay_methods=("A", "B"), threshold_pct=D("0.5"))
    opp = find_best_opportunity(buy_ads, sell_ads, t, NOW)
    assert opp is not None
    # Mejor combinación: comprar con A a 790, vender con B a 825.
    assert opp.buy_pay_method == "A"
    assert opp.sell_pay_method == "B"
    assert opp.buy_adv_no == "bA"
    assert opp.sell_adv_no == "sB"


def test_opportunity_carries_links_and_advertisers():
    buy_ads = [make_ad(adv_no="b1", price="800", trade_type="BUY")]
    sell_ads = [make_ad(adv_no="s1", price="830", trade_type="SELL")]
    opp = find_best_opportunity(buy_ads, sell_ads, target(), NOW)
    # Sin filtro de método => libro en vivo de cada lado con payment=all-payments.
    assert "/trade/buy/USDT?fiat=VES" in opp.buy_url
    assert "/trade/sell/USDT?fiat=VES" in opp.sell_url
    assert "payment=all-payments" in opp.buy_url
    assert opp.buy_advertiser == "name-b1"


def test_opportunity_links_carry_pay_method():
    buy_ads = [make_ad(adv_no="bA", price="800", trade_type="BUY", methods=("Banesco",))]
    sell_ads = [make_ad(adv_no="sA", price="830", trade_type="SELL", methods=("Banesco",))]
    t = target(pay_methods=("Banesco",))
    opp = find_best_opportunity(buy_ads, sell_ads, t, NOW)
    assert "/trade/buy/USDT?fiat=VES&payment=Banesco" in opp.buy_url
    assert "/trade/sell/USDT?fiat=VES&payment=Banesco" in opp.sell_url


def test_best_spread_without_threshold():
    buy_ads = [make_ad(adv_no="b1", price="800", trade_type="BUY")]
    sell_ads = [make_ad(adv_no="s1", price="808", trade_type="SELL")]  # 1%
    assert best_spread(buy_ads, sell_ads, target()) == D("1")


def test_best_spread_none_when_no_eligible():
    assert best_spread([], [], target()) is None


# --- filtro de outliers ------------------------------------------------------

def test_drop_outliers_removes_bait_price():
    ads = [
        make_ad(adv_no="a", price="785", trade_type="SELL"),
        make_ad(adv_no="b", price="790", trade_type="SELL"),
        make_ad(adv_no="c", price="788", trade_type="SELL"),
        make_ad(adv_no="bait", price="1780", trade_type="SELL"),  # ~2.25x mediana
    ]
    kept = {a.adv_no for a in drop_outliers(ads, D("15"))}
    assert "bait" not in kept
    assert kept == {"a", "b", "c"}


def test_drop_outliers_disabled_when_zero():
    ads = [
        make_ad(adv_no="a", price="785", trade_type="SELL"),
        make_ad(adv_no="b", price="790", trade_type="SELL"),
        make_ad(adv_no="bait", price="1780", trade_type="SELL"),
    ]
    assert len(drop_outliers(ads, D("0"))) == 3


def test_drop_outliers_skips_with_few_ads():
    # Con menos de 3 anuncios no se puede distinguir el outlier de forma fiable.
    ads = [
        make_ad(adv_no="a", price="785", trade_type="SELL"),
        make_ad(adv_no="bait", price="1780", trade_type="SELL"),
    ]
    assert len(drop_outliers(ads, D("15"))) == 2


def test_outlier_filter_blocks_fake_opportunity():
    # El anuncio cebo de venta a 1780 generaría un spread del 127% sin el filtro.
    buy_ads = [
        make_ad(adv_no="b1", price="780", trade_type="BUY"),
        make_ad(adv_no="b2", price="782", trade_type="BUY"),
        make_ad(adv_no="b3", price="784", trade_type="BUY"),
    ]
    sell_ads = [
        make_ad(adv_no="s1", price="786", trade_type="SELL"),
        make_ad(adv_no="s2", price="788", trade_type="SELL"),
        make_ad(adv_no="bait", price="1780", trade_type="SELL"),
    ]
    t = target(threshold_pct=D("1.0"), outlier_max_dev_pct=D("15"))
    opp = find_best_opportunity(buy_ads, sell_ads, t, NOW)
    # La mejor venta real (788) vs mejor compra (780) = 1.025% > umbral 1%.
    assert opp is not None
    assert opp.sell_price == D("788")
    assert opp.sell_adv_no != "bait"


def test_without_filter_bait_produces_fake_spread():
    # Mismo libro, filtro desactivado: el cebo gana y produce el spread falso.
    buy_ads = [make_ad(adv_no="b1", price="780", trade_type="BUY")]
    sell_ads = [
        make_ad(adv_no="s1", price="788", trade_type="SELL"),
        make_ad(adv_no="bait", price="1780", trade_type="SELL"),
    ]
    t = target(threshold_pct=D("1.0"), outlier_max_dev_pct=D("0"))
    opp = find_best_opportunity(buy_ads, sell_ads, t, NOW)
    assert opp is not None
    assert opp.sell_adv_no == "bait"


# --- ganancia real (operación ya ejecutada a mano) --------------------------

def test_realized_pnl_gain():
    # Pagó 3600 VES por 100 USDT y recibió 3700 al venderlos.
    r = realized_pnl(fiat_in=D("3600"), fiat_out=D("3700"), usdt=D("100"))
    assert r.profit_fiat == D("100")
    assert round(r.net_pct, 3) == D("2.778")
    # 100 VES de ganancia valen 100/36 = 2.78 USDT al precio de compra real.
    assert round(r.profit_usdt, 3) == D("2.778")


def test_realized_pnl_loss():
    r = realized_pnl(fiat_in=D("3600"), fiat_out=D("3500"), usdt=D("100"))
    assert r.profit_fiat == D("-100")
    assert r.net_pct < 0


def test_realized_pnl_uses_fiat_not_prices():
    """Las comisiones se comen el spread: el % real sale de lo que movió el banco.

    Los precios nominales darían 2.778%, pero si al vender solo entraron 3650
    (comisión mediante), lo real es la mitad. Ese hueco es lo que interesa medir.
    """
    r = realized_pnl(fiat_in=D("3600"), fiat_out=D("3650"), usdt=D("100"))
    assert round(r.net_pct, 3) == D("1.389")


def test_realized_pnl_without_investment_is_zero():
    # Sin fiat invertido no hay % que calcular: cero, no división por cero.
    r = realized_pnl(fiat_in=D("0"), fiat_out=D("100"), usdt=D("10"))
    assert r.profit_fiat == D("0")
    assert r.net_pct == D("0")
    assert r.profit_usdt == D("0")


def test_venta_valida_unidades_y_fiat_de_la_compra():
    """El fondo compra 10 unidades: la venta debe aceptar 10 y 1100 fiat."""
    buy = make_ad(adv_no="b", price="100", trade_type="BUY")
    t = target(max_fiat=D("1000"))
    cases = [
        ({"surplus": "9.5"}, False),
        ({"max_amount": "1050"}, False),
        ({"min_amount": "1050"}, True),
        ({"min_amount": "1100", "max_amount": "1100", "surplus": "10"}, True),
    ]
    for limits, expected in cases:
        sell = make_ad(adv_no="s", price="110", trade_type="SELL", **limits)
        opp = find_best_opportunity([buy], [sell], t, NOW)
        assert (opp is not None) == expected, limits
        assert best_spread([buy], [sell], t) == (D("10") if expected else None)
        if opp:
            assert opp.max_usdt == D("10")


def test_compra_alternativa_si_la_mas_barata_no_se_puede_vender():
    buys = [make_ad(adv_no="barata", price="100", trade_type="BUY"),
            make_ad(adv_no="viable", price="125", trade_type="BUY")]
    sell = make_ad(adv_no="s", price="150", trade_type="SELL", surplus="8")
    t = target(max_fiat=D("1000"))
    opp = find_best_opportunity(buys, [sell], t, NOW)
    assert opp is not None
    assert opp.buy_adv_no == "viable"
    assert opp.max_usdt == D("8")
    assert best_spread(buys, [sell], t) == opp.net_pct == D("20")
