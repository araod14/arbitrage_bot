"""Cola temporal: expiración, exclusión, recuperación e idempotencia."""

import pytest

from p2p_arb_bot.infrastructure.control_store import ControlStore


def test_deduplica_solicitud_y_limita_frecuencia(tmp_path):
    store = ControlStore(str(tmp_path / "control.db"))
    rid = store.enqueue({"route_key": "ruta", "funds": "100"})
    assert store.enqueue({"route_key": "ruta", "funds": "100"}) == rid
    with pytest.raises(ValueError, match="curso"):
        store.enqueue({"route_key": "ruta", "funds": "200"})
    with pytest.raises(ValueError, match="Espera"):
        store.enqueue({"route_key": "otra"})


def test_claim_exclusivo_y_recuperacion_no_acepta_worker_anterior(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("p2p_arb_bot.infrastructure.control_store.time.time", lambda: clock[0])
    store = ControlStore(str(tmp_path / "control.db"))
    store.enqueue({"route_key": "ruta"})
    first = store.claim()
    assert first is not None and store.claim() is None
    clock[0] += 31
    recovered = ControlStore(store.path).claim()
    assert recovered["id"] == first["id"] and recovered["lease"] != first["lease"]
    store.finish(first, {"message": "obsoleto"})
    assert store.recent()[0]["state"] == "processing"
    store.finish(recovered, {"message": "nuevo"})
    assert store.recent()[0]["result"]["message"] == "nuevo"


def test_solicitudes_expiradas_no_se_procesan(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("p2p_arb_bot.infrastructure.control_store.time.time", lambda: clock[0])
    store = ControlStore(str(tmp_path / "control.db"), ttl_s=10)
    store.enqueue({"route_key": "ruta"})
    request = store.claim()
    clock[0] += 11
    store.finish(request, {"message": "tarde"})
    assert store.recent()[0]["state"] == "expired"
    assert store.claim() is None


def test_limite_pendientes_y_retencion(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("p2p_arb_bot.infrastructure.control_store.time.time", lambda: clock[0])
    store = ControlStore(str(tmp_path / "control.db"), max_pending=1, cooldown_s=0, retention_days=1)
    store.enqueue({"route_key": "ruta"})
    with pytest.raises(ValueError, match="demasiadas"):
        store.enqueue({"route_key": "otra"})
    clock[0] += 86401
    assert store.recent() == []


def test_metricas_miden_consultas_no_ganancias(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("p2p_arb_bot.infrastructure.control_store.time.time", lambda: clock[0])
    store = ControlStore(str(tmp_path / "control.db"), cooldown_s=0)
    store.enqueue({"route_key": "ruta"})
    request = store.claim()
    clock[0] += 4
    store.finish(request, {"state": "compatible"})
    store.enqueue({"route_key": "otra"})
    request = store.claim()
    clock[0] += 2
    store.finish(request, {"state": "funds"})
    metrics = store.metrics()
    assert metrics["favorable_pct"] == 50 and metrics["avg_latency_s"] == 3
    assert metrics["reasons"] == {"compatible": 1, "funds": 1}
