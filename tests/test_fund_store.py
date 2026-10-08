"""Reservas transaccionales y persistencia privada con SQLite temporal."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from p2p_arb_bot.web.fund_store import FundStore


def seed(store):
    return store.save(name="Banco", fiat="VES", balance="100", methods="Banesco,PagoMovil",
                      can_receive=True)


def test_persistencia_reservas_y_liberacion_idempotente(tmp_path):
    path = str(tmp_path / "funds.db")
    store = FundStore(path)
    account_id = seed(store)
    reserve_id = store.reserve(account_id, "60", expected_version=1)
    accounts, revision = FundStore(path).snapshot()
    assert accounts[0].available == 40 and revision == 2
    store.release(reserve_id)
    store.release(reserve_id)
    accounts, revision = store.snapshot()
    assert accounts[0].available == 100 and revision == 3
    assert len(store.changes()) == 3


def test_no_permite_actualizar_saldo_bajo_reservas_ni_archivar_con_reservas(tmp_path):
    store = FundStore(str(tmp_path / "funds.db"))
    aid = seed(store)
    store.reserve(aid, "60", expected_version=1)
    with pytest.raises(ValueError, match="reservas"):
        store.save(name="Banco", fiat="VES", balance="59", methods="Banesco", can_receive=True,
                   account_id=aid, expected_version=2)
    with pytest.raises(ValueError, match="reservas"):
        store.archive(aid, 2)
    assert store.snapshot()[0][0].balance == 100


def test_reservas_concurrentes_no_duplican_capital(tmp_path):
    store = FundStore(str(tmp_path / "funds.db"))
    aid = seed(store)

    def reserve():
        try:
            store.reserve(aid, "80", expected_version=1)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sorted(results) == [False, True]
    assert store.snapshot()[0][0].available == 20


def test_cambio_en_receptor_invalida_revision_global(tmp_path):
    store = FundStore(str(tmp_path / "funds.db"))
    aid = seed(store)
    _, revision = store.snapshot()
    store.save(name="Receptor", fiat="VES", balance="0", methods="Mercantil", can_receive=True)
    with pytest.raises(ValueError, match="fondos cambiaron"):
        store.reserve(aid, "10", expected_version=1, expected_revision=revision)


def test_reserva_por_comprobacion_es_idempotente(tmp_path):
    store = FundStore(str(tmp_path / "funds.db"))
    aid = seed(store)
    first = store.reserve(aid, "10", expected_version=1, source_request="comprobacion")
    assert store.reserve(aid, "10", expected_version=1, source_request="comprobacion") == first
    assert len(store.reservations()) == 1
    with pytest.raises(ValueError):
        store.reserve(aid, "20", expected_version=2, source_request="comprobacion")


def test_archivar_preserva_historial(tmp_path):
    store = FundStore(str(tmp_path / "funds.db"))
    aid = seed(store)
    store.archive(aid, 1)
    assert store.snapshot()[0] == []
    assert len(store.changes()) == 2


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "abc"])
def test_rechaza_saldos_invalidos(tmp_path, amount):
    store = FundStore(str(tmp_path / "funds.db"))
    with pytest.raises(ValueError):
        store.save(name="Banco", fiat="VES", balance=amount, methods="Banesco", can_receive=True)
