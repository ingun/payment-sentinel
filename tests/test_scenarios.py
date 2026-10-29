"""Senaryo matrisi: her etiketli senaryo her iki mimaride uçtan uca çalışır ve etiketlerle tutarlıdır.

Ayrıca veri kalitesi ve hacim düşüşü senaryolarının özel davranışları test edilir.
"""

import pytest

from src.agent import InvestigationAgent
from src.data_generator import SCENARIO_LABELS, SCENARIOS
from src.multiagent import Orchestrator
from src.tools import PaymentToolkit
from tests.conftest import CURRENT, pair

ARCHITECTURES = ["single", "multi"]


def detection_key(d: dict) -> tuple:
    """Etiket karşılaştırma anahtarı (scripts/evaluate.py ile aynı kural)."""
    if d["detector"] == "SEGMENT_ISOLATION":
        dim = d["extra"].get("dimension")
        return d["detector"], ((dim, d["scope"].get(dim)),)
    if d["detector"] == "RESPONSE_CODE_CONCENTRATION":
        return d["detector"], (("response_code", d["subject"]),)
    if d["detector"] == "SERVICE_VERSION_DEGRADATION":
        return d["detector"], (("service_version", d["subject"]),)
    return d["detector"], ()


def label_key(item: dict) -> tuple:
    return item["detector"], tuple(sorted(item.get("scope", {}).items()))


@pytest.fixture(scope="module")
def outcomes(repos, retriever):
    out = {}
    for s in SCENARIOS:
        request = SCENARIO_LABELS[s]["request"]
        out[(s, "single")] = InvestigationAgent(PaymentToolkit(repos[s], retriever)).run(request, thread_id=s)
        out[(s, "multi")] = Orchestrator(PaymentToolkit(repos[s], retriever)).run(request)
    return out


@pytest.mark.parametrize("arch", ARCHITECTURES)
@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_scenario_matches_labels(outcomes, scenario, arch):
    label = SCENARIO_LABELS[scenario]
    rep = outcomes[(scenario, arch)]["report"]
    found = {detection_key(d) for d in rep["detections"]}
    expected = {label_key(x) for x in label["expected"]}
    acceptable = {label_key(x) for x in label.get("acceptable", [])}
    assert expected <= found, f"eksik tespit: {expected - found}"
    assert not found - expected - acceptable, f"etiket dışı tespit: {found - expected - acceptable}"
    assert rep["risk"]["severity"] in label["severities"]
    assert (rep["status"] == "insufficient_evidence") == label["insufficient"]
    if "evidence_quality" in label:
        assert rep["risk"]["evidence_quality"] == label["evidence_quality"]
    assert all(v["passed"] for v in rep["validation"]), [v for v in rep["validation"] if not v["passed"]]


def test_both_architectures_agree_on_risk(outcomes):
    for s in SCENARIOS:
        single, multi = outcomes[(s, "single")]["report"]["risk"], outcomes[(s, "multi")]["report"]["risk"]
        assert (single["severity"], single["score"]) == (multi["severity"], multi["score"]), s


# ---------- Veri kalitesi ----------


def test_data_quality_gap_is_reported_not_claimed_as_anomaly(outcomes, make_toolkit):
    cov = make_toolkit("data_quality_gap").call("check_data_coverage", {"window": CURRENT})
    assert cov["evidence_quality"] == "limited"
    assert cov["empty_buckets"] >= 3 and cov["coverage_ratio"] < 0.8
    assert cov["null_rates"]["bank_id"] > 0.02 and cov["null_rates"]["correlation_id"] > 0.02
    assert any("Zaman kapsamı eksik" in i for i in cov["issues"]) and any("boş değer" in i for i in cov["issues"])

    for arch in ARCHITECTURES:
        rep = outcomes[("data_quality_gap", arch)]["report"]
        assert rep["detections"] == [] and rep["risk"]["evidence_quality"] == "limited"
        assert any("Zaman kapsamı eksik" in x for x in rep["limitations"])
    pf = next(a for a in outcomes[("data_quality_gap", "multi")]["agent_results"] if a["agent_name"] == "payment_flow")
    assert any("correlation_id" in x for x in pf["limitations"])


def test_reversals_without_correlation_id_are_unlinkable_not_orphans(retriever):
    from src.data_generator import generate_scenario
    from src.repositories import SyntheticPaymentRepository

    events = generate_scenario("bank_timeout")
    window = [e for e in events if e["transaction_type"] == "reversal" and CURRENT["start"] <= e["timestamp"] < CURRENT["end"]]
    for e in window[:12]:  # log hattı bu reversal'ların correlation_id alanını kaybetti
        e["correlation_id"] = None
    tk = PaymentToolkit(SyntheticPaymentRepository.from_records(events), retriever)
    out = tk.call("analyze_reversals", pair())
    cats = out["current"]["categories"]
    assert cats["unlinkable"] == 12
    assert cats["orphan"] < 12  # boş anahtarlar orphan sayılmaz ve birbirleriyle eşleşmez
    assert not out["detection"]["detected"]  # veri kalitesi sorunu sahte reversal anomalisi üretmez
    assert any("correlation_id alanı boş" in i for i in out["reconciliation_risk"]["indicators"])


def test_coverage_gap_check_ignores_naturally_sparse_low_volume(make_toolkit):
    cov = make_toolkit("low_volume").call("check_data_coverage", {"window": CURRENT})
    assert not any("Zaman kapsamı eksik" in i for i in cov["issues"])  # boş dilimler düşük hacimde doğaldır
    assert cov["evidence_quality"] == "insufficient"  # neden: örneklem


# ---------- Hacim düşüşü ----------


def test_volume_drop_end_to_end(outcomes, make_toolkit):
    out = make_toolkit("volume_drop").call("compare_with_baseline", pair())
    vol = next(d for d in out["detections"] if d["detector"] == "VOLUME_ANOMALY")
    assert vol["detected"] and vol["subject"] == "düşüş" and vol["extra"]["reference"] == "day_over_day"
    assert vol["extra"]["missing_transactions"] > 100
    tech = next(d for d in out["detections"] if d["detector"] == "TECHNICAL_FAILURE_SPIKE")
    assert not tech["detected"]  # hata oranı normal
    risk = outcomes[("volume_drop", "single")]["report"]["risk"]
    factors = {f["factor"] for f in risk["factors"]}
    assert {"Hacim düşüşü", "Etkilenen işlem"} <= factors
    assert risk["affected_transactions"] == vol["extra"]["missing_transactions"]
