"""Selección interactiva sin consultas de red ni entrada real."""

import asyncio

import pytest

from p2p_arb_bot import main
from p2p_arb_bot.config import Defaults, validate_storage_paths


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


def test_bases_privadas_no_pueden_compartir_fichero_de_mercado(tmp_path):
    db = str(tmp_path / "market.db")
    with pytest.raises(ValueError, match="ficheros distintos"):
        validate_storage_paths(db, db, str(tmp_path / "trades.db"))


def test_nuevos_defaults_respetan_env_path_y_frescura_por_intervalo(tmp_path, monkeypatch):
    env = tmp_path / "empty.env"
    env.write_text("")
    monkeypatch.setenv("ENV_PATH", str(env))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "market.db"))
    monkeypatch.setenv("POLL_INTERVAL_S", "45")
    monkeypatch.delenv("CONTROL_DB_PATH", raising=False)
    monkeypatch.delenv("MARKET_FRESHNESS_S", raising=False)
    defaults = Defaults.from_env()
    assert defaults.market_freshness_s == 90
    assert defaults.control_db_path == str(tmp_path / "control.db")
