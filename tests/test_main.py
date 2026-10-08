"""Selección interactiva sin consultas de red ni entrada real."""

import asyncio

import pytest

from p2p_arb_bot import main
from p2p_arb_bot.config import Defaults


@pytest.mark.parametrize("falla_primera", [False, True])
def test_menu_combina_metodos_de_todas_las_monedas(monkeypatch, falla_primera):
    calls = []

    async def discover(source, asset, fiat):
        calls.append(asset)
        if asset == "USDT":
            if falla_primera:
                raise TimeoutError("sin respuesta")
            return [("Banesco", "Banesco"), ("PagoMovil", "PagoMovil")]
        return [("Banesco", "Banesco"), ("Mercantil", "Mercantil")]

    async def ask(*args):
        return "1,2,3"

    monkeypatch.setattr(main, "discover_pay_methods", discover)
    monkeypatch.setattr(main, "_ask", ask)
    methods = asyncio.run(main._select_pay_methods(
        object(), Defaults(assets=("USDT", "BTC")),
    ))
    assert calls == ["USDT", "BTC"]
    expected = ("Banesco", "Mercantil") if falla_primera else ("Banesco", "Mercantil", "PagoMovil")
    assert methods == expected
