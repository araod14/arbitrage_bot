"""Tests del caso de uso ``MonitorService`` con puertos falsos (sin red/disco).

Se usa ``asyncio.run`` para no añadir dependencia de pytest-asyncio.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from p2p_arb_bot.application.monitor import MonitorService
from p2p_arb_bot.domain.models import Ad, Opportunity, TradeType, WatchTarget

D = Decimal


def make_ad(adv_no: str, price: str, trade_type: TradeType) -> Ad:
    return Ad(
        adv_no=adv_no,
        price=D(price),
        surplus=D("10000"),
        min_amount=D("0"),
        max_amount=D("1000000"),
        pay_methods=("PagoMovil",),
        advertiser_no=f"user-{adv_no}",
        advertiser_name=f"name-{adv_no}",
        trade_type=trade_type,
    )


class FakeSource:
    def __init__(self, buy_ads: list[Ad], sell_ads: list[Ad]) -> None:
        self._buy = buy_ads
        self._sell = sell_ads
        self.calls: list[TradeType] = []

    async def fetch_ads(self, *, trade_type: TradeType, **_kw) -> list[Ad]:
        self.calls.append(trade_type)
        return self._buy if trade_type == "BUY" else self._sell

    async def aclose(self) -> None:  # pragma: no cover - no usado en tests
        pass


class FakeRepo:
    def __init__(self, duplicate: bool = False) -> None:
        self.saved: list[Opportunity] = []
        self._duplicate = duplicate
        self.dup_checks = 0

    def save(self, opp: Opportunity) -> None:
        self.saved.append(opp)

    def recent_duplicate(self, opp: Opportunity, window_s: int) -> bool:
        self.dup_checks += 1
        return self._duplicate

    def close(self) -> None:  # pragma: no cover
        pass


class FakeNotifier:
    def __init__(self) -> None:
        self.opportunities: list[Opportunity] = []
        self.heartbeats: list[Decimal | None] = []

    async def notify_opportunity(self, opp: Opportunity) -> None:
        self.opportunities.append(opp)

    async def notify_heartbeat(self, target: WatchTarget, best_spread) -> None:
        self.heartbeats.append(best_spread)


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


def test_persists_and_notifies_on_opportunity():
    source = FakeSource(
        buy_ads=[make_ad("b1", "800", "BUY")],
        sell_ads=[make_ad("s1", "820", "SELL")],  # 2.5%
    )
    repo, notifier = FakeRepo(), FakeNotifier()
    service = MonitorService(source, [repo], [notifier])

    opp = asyncio.run(service.run_cycle(target()))

    assert opp is not None
    assert len(repo.saved) == 1
    assert len(notifier.opportunities) == 1
    assert notifier.opportunities[0].net_pct == D("2.5")
    # Pidió ambos lados.
    assert set(source.calls) == {"BUY", "SELL"}


def test_suppresses_recent_duplicate():
    source = FakeSource(
        buy_ads=[make_ad("b1", "800", "BUY")],
        sell_ads=[make_ad("s1", "820", "SELL")],
    )
    repo, notifier = FakeRepo(duplicate=True), FakeNotifier()
    service = MonitorService(source, [repo], [notifier])

    opp = asyncio.run(service.run_cycle(target()))

    assert opp is None
    assert repo.saved == []
    assert notifier.opportunities == []
    assert repo.dup_checks == 1


def test_heartbeat_when_no_opportunity():
    source = FakeSource(
        buy_ads=[make_ad("b1", "800", "BUY")],
        sell_ads=[make_ad("s1", "804", "SELL")],  # 0.5% < umbral
    )
    repo, notifier = FakeRepo(), FakeNotifier()
    service = MonitorService(source, [repo], [notifier])

    opp = asyncio.run(service.run_cycle(target()))

    assert opp is None
    assert repo.saved == []
    assert notifier.opportunities == []
    assert notifier.heartbeats == [D("0.5")]


def test_multiple_repos_and_notifiers_all_receive():
    source = FakeSource(
        buy_ads=[make_ad("b1", "800", "BUY")],
        sell_ads=[make_ad("s1", "820", "SELL")],
    )
    repos = [FakeRepo(), FakeRepo()]
    notifiers = [FakeNotifier(), FakeNotifier()]
    service = MonitorService(source, repos, notifiers)  # type: ignore[arg-type]

    asyncio.run(service.run_cycle(target()))

    assert all(len(r.saved) == 1 for r in repos)
    assert all(len(n.opportunities) == 1 for n in notifiers)
