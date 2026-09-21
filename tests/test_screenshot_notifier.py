"""Tests del ``ScreenshotNotifier`` (adaptador de I/O: usa disco temporal)."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal

from PIL import Image

from p2p_arb_bot.domain.models import Opportunity, WatchTarget
from p2p_arb_bot.infrastructure.screenshot_notifier import ScreenshotNotifier


def _opp() -> Opportunity:
    return Opportunity(
        detected_at=datetime(2026, 7, 9, 18, 3, 22, tzinfo=timezone.utc),
        fiat="VES", asset="USDT",
        buy_pay_method="PagoMovil", sell_pay_method="Banesco",
        buy_price=Decimal("36.5"), sell_price=Decimal("37.4"),
        spread_pct=Decimal("2.466"), net_pct=Decimal("2.2"),
        max_usdt=Decimal("100"),
        buy_adv_no="111", sell_adv_no="222",
        buy_advertiser="Juan", sell_advertiser="Ana",
        buy_url="https://p2p.binance.com/es/trade/buy/USDT?fiat=VES&payment=PagoMovil",
        sell_url="https://p2p.binance.com/es/trade/sell/USDT?fiat=VES&payment=Banesco",
        est_profit_usdt=Decimal("2.2"),
    )


def test_guarda_un_png_valido_con_nombre_fecha_id(tmp_path):
    notifier = ScreenshotNotifier(str(tmp_path))
    opp = _opp()
    asyncio.run(notifier.notify_opportunity(opp))

    files = list(tmp_path.glob("*.png"))
    assert len(files) == 1
    name = files[0].name
    # AAAAMMDD_HHMMSS_ASSET-FIAT_id.png (fecha/hora local + id de 8 hex).
    assert re.fullmatch(r"\d{8}_\d{6}_USDT-VES_[0-9a-f]{8}\.png", name)

    # Es un PNG válido y legible.
    assert files[0].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    with Image.open(files[0]) as img:
        assert img.format == "PNG"
        assert img.width > 0 and img.height > 0


def test_deshabilitado_no_crea_archivos(tmp_path):
    notifier = ScreenshotNotifier(str(tmp_path), enabled=False)
    asyncio.run(notifier.notify_opportunity(_opp()))
    assert list(tmp_path.iterdir()) == []


def test_heartbeat_es_noop(tmp_path):
    notifier = ScreenshotNotifier(str(tmp_path))
    target = WatchTarget(
        asset="USDT", fiat="VES", pay_methods=(),
        max_fiat=Decimal("80000"), threshold_pct=Decimal("1"),
    )
    asyncio.run(notifier.notify_heartbeat(target, Decimal("1.0")))
    assert list(tmp_path.iterdir()) == []


def test_error_de_escritura_no_lanza(tmp_path):
    # El "directorio" es en realidad un fichero: cualquier escritura falla, pero
    # el notifier debe tragarse el error para no tumbar el ciclo del bot.
    fake_dir = tmp_path / "no_soy_dir"
    fake_dir.write_text("x", encoding="utf-8")
    notifier = ScreenshotNotifier(str(fake_dir))
    asyncio.run(notifier.notify_opportunity(_opp()))  # no debe lanzar
