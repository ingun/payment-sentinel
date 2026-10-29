"""Streamlit arayüzü için başsız (headless) testler: her iki mimari, hata simülasyonu ve kanıt yetersizliği."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")
START = "İncelemeyi başlat"


def start(scenario: str, arch: str = "multi", fault: str | None = None) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=120)
    at.query_params.update({"scenario": scenario, "provider": "none", "arch": arch, **({"fault": fault} if fault else {})})
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    next(b for b in at.button if b.label == START).click().run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def metric(at: AppTest, label: str) -> str:
    return next(m.value for m in at.metric if m.label == label)


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "none")


def test_multi_agent_run_renders_agents_tab_and_report():
    at = start("bank_timeout")
    assert len(at.tabs) == 14
    assert metric(at, "Agent çalıştırması") == "4" and metric(at, "Tur") == "1"
    assert any("eşzamanlı" in m.value for m in at.markdown)  # paralel dalga izlerde görünür
    assert any("Risk: HIGH" in str(e.proto) for e in at.error)
    assert len(at.get("download_button")) == 2


def test_follow_up_kpis_use_investigation_scope_not_follow_up_scope():
    at = start("reversal_anomaly")
    assert metric(at, "Tur") == "2"
    assert float(metric(at, "İşlem / dk")) > 10  # M-007 takip turundaki dar kapsam değil, tüm trafik


def test_single_agent_architecture_and_insufficient_evidence():
    at = start("low_volume", arch="single")
    assert any("Kanıt yetersiz" in str(i.proto) for i in at.info)
    assert any("tek agent" in c.value for c in at.caption)  # Agent'lar sekmesi tek agent modunu açıklar


def test_tool_failure_is_disclosed_in_ui():
    at = start("bank_timeout", fault="runbook_error")
    assert any("İnceleme eksik" in str(w.proto) for w in at.warning)
