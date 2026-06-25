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
) -> Ad:
    return Ad(
        adv_no=adv_no,
        price=D(price),
        surplus=D("10000"),
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
        max_usdt=D("100"),
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
    assert best_buy(ads, D("100")).adv_no == "b"


def test_best_sell_picks_highest_price():
    ads = [
        make_ad(adv_no="a", price="800", trade_type="SELL"),
        make_ad(adv_no="b", price="820", trade_type="SELL"),
        make_ad(adv_no="c", price="810", trade_type="SELL"),
    ]
    assert best_sell(ads, D("100")).adv_no == "b"


def test_best_buy_returns_none_when_empty():
    assert best_buy([], D("100")) is None


# --- accepts_amount / filtrado por límites -----------------------------------

def test_accepts_amount_within_limits():
    ad = make_ad(adv_no="a", price="800", trade_type="BUY",
                 min_amount="10000", max_amount="100000")
    # 100 USDT * 800 = 80_000 VES, dentro de [10_000, 100_000]
    assert accepts_amount(ad, D("100"), D("800")) is True


def test_accepts_amount_below_min_rejected():
    ad = make_ad(adv_no="a", price="800", trade_type="BUY",
                 min_amount="200000", max_amount="500000")
    # 80_000 < 200_000 -> rechazado
    assert accepts_amount(ad, D("100"), D("800")) is False


def test_accepts_amount_above_max_rejected():
    ad = make_ad(adv_no="a", price="800", trade_type="BUY",
                 min_amount="0", max_amount="50000")
    # 80_000 > 50_000 -> rechazado
    assert accepts_amount(ad, D("100"), D("800")) is False


def test_accepts_amount_boundaries_inclusive():
    ad = make_ad(adv_no="a", price="800", trade_type="BUY",
                 min_amount="80000", max_amount="80000")
    assert accepts_amount(ad, D("100"), D("800")) is True


def test_best_buy_skips_ads_outside_limits():
    ads = [
        # Precio más bajo pero su max no admite el monto -> se descarta.
        make_ad(adv_no="cheap", price="790", trade_type="BUY",
                min_amount="0", max_amount="1000"),
        make_ad(adv_no="ok", price="800", trade_type="BUY",
                min_amount="0", max_amount="1000000"),
    ]
    assert best_buy(ads, D("100")).adv_no == "ok"


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


def test_est_profit_usdt_from_net_pct():
    buy_ads = [make_ad(adv_no="b1", price="800", trade_type="BUY")]
    sell_ads = [make_ad(adv_no="s1", price="816", trade_type="SELL")]  # 2% bruto
    # buffer 0.5% -> net 1.5%; sobre 200 USDT -> 3 USDT estimados.
    t = target(max_usdt=D("200"), threshold_pct=D("1.0"), fee_buffer_pct=D("0.5"))
    opp = find_best_opportunity(buy_ads, sell_ads, t, NOW)
    assert opp is not None
    assert opp.net_pct == D("1.5")
    assert opp.est_profit_usdt == D("3.0")


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
    assert "advertiserNo=user-b1" in opp.buy_url
    assert "advertiserNo=user-s1" in opp.sell_url
    assert opp.buy_advertiser == "name-b1"


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
