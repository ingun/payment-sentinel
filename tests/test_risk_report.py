"""Risk motoru ve rapor tutarlılığı testleri."""

import copy

import pytest

from src.agent import InvestigationAgent
from src.analytics.thresholds import RiskThresholds
from src.data_generator import SCENARIO_LABELS, SCENARIOS
from src.redaction import mask_terminal, redact_record, redact_text
from src.report import check_report, validate_agent_findings
from src.risk_engine import assess_risk
from src.schemas import InvestigationReport


@pytest.fixture(scope="module")
def runs(repos, retriever):
    from src.tools import PaymentToolkit

    out = {}
    for s in SCENARIOS:
        st = InvestigationAgent(PaymentToolkit(repos[s], retriever)).run(SCENARIO_LABELS[s]["request"], thread_id=s)
        out[s] = st
    return out


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_severity_matches_labels(runs, scenario):
    assert runs[scenario]["report"]["risk"]["severity"] in SCENARIO_LABELS[scenario]["severities"]


def test_normal_scenarios_never_high(runs):
    for s in ("normal_baseline", "normal_fluctuation"):
        assert runs[s]["report"]["risk"]["severity"] == "LOW"
        assert runs[s]["report"]["detections"] == []


def test_insufficient_evidence_is_explicit(runs):
    rep = runs["low_volume"]["report"]
    assert rep["status"] == "insufficient_evidence" and rep["risk"]["severity"] == "INDETERMINATE"
    assert rep["risk"]["score"] is None and rep["hypotheses"] == []
    assert "Kanıt yetersiz" in rep["executive_summary"][0]


def test_score_equals_factor_sum_and_monetary_impact_not_invented(runs):
    for st in runs.values():
        risk = st["report"]["risk"]
        if risk["score"] is not None:
            assert risk["score"] == min(100, sum(f["points"] for f in risk["factors"]))
        assert risk["monetary_impact"].startswith("Belirlenemedi")


def test_risk_is_monotonic_in_failure_delta(runs):
    st = runs["bank_timeout"]
    results = copy.deepcopy(st["results"])
    base = next(r for r in results if r["tool"] == "compare_with_baseline")
    original = assess_risk(results, st["scope"]).score
    base["output"]["deltas"]["technical_pp"] = 1.5
    assert assess_risk(results, st["scope"]).score <= original


def test_limited_evidence_caps_severity(runs):
    st = runs["bank_timeout"]
    results = copy.deepcopy(st["results"])
    tech = next(d for d in next(r for r in results if r["tool"] == "compare_with_baseline")["output"]["detections"]
                if d["detector"] == "TECHNICAL_FAILURE_SPIKE")
    tech["evidence_quality"] = "limited"
    risk = assess_risk(results, st["scope"])
    assert risk.severity == "MEDIUM" and risk.capped


def test_tool_failures_lower_confidence_not_score(runs):
    st = runs["bank_timeout"]
    clean = assess_risk(st["results"], st["scope"])
    failed = assess_risk(st["results"], st["scope"], tool_failures=2, missing=["search_runbooks"])
    assert failed.score == clean.score and failed.confidence == "LOW"


def test_thresholds_change_score():
    rt = RiskThresholds(high_threshold=90)
    assert rt.high_threshold == 90


def test_report_has_all_sections_and_traceable_facts(runs):
    rep = runs["bank_timeout"]["report"]
    md = runs["bank_timeout"]["report_markdown"]
    for i in range(1, 15):
        assert f"## {i}." in md
    assert all(f["source"].startswith("adım ") for f in rep["facts"])
    assert rep["confirmed_causes"] == []
    assert all(h["evidence_refs"] for h in rep["hypotheses"])
    assert all(r["source"] != "-" or r["evidence_refs"] for r in rep["recommendations"])
    assert all(v["passed"] for v in rep["validation"])
    assert "SENTETİK VERİ" in md


def test_report_key_metrics_match_tool_output_and_tampering_is_caught(runs):
    st = runs["bank_timeout"]
    report = InvestigationReport(**st["report"])
    assert check_report(report, st["results"]) == []
    report.key_metrics[2]["current"] = "%0.01"
    report.facts[0].text += " Ayrıca 987654 işlem etkilendi."
    issues = check_report(report, st["results"])
    assert any("temel metrikler" in i for i in issues) and any("doğrulanmayan sayı" in i for i in issues)


def test_llm_findings_are_validated(runs):
    st = runs["bank_timeout"]
    accepted, rejected = validate_agent_findings([
        "BANK_B'de timeout oranı %14.22 oldu.",
        "Toplam 12345 işlem başarısız oldu.",
        "Kök neden doğrulandı: BANK_B HSM arızası.",
    ], st["results"], st["scope"])
    assert accepted == ["BANK_B'de timeout oranı %14.22 oldu."]
    assert len(rejected) == 2


def test_redaction():
    assert redact_text("kart 4111 1111 1111 1111 ve test@example.com") == "kart ****1111 ve [e-posta maskelendi]"
    assert redact_text("SYN-TX-8490-0006258 2026-10-27T11:00:00Z") == "SYN-TX-8490-0006258 2026-10-27T11:00:00Z"
    assert mask_terminal("T-007-3") == "T-***-3"
    rec = redact_record({"transaction_id": "x", "terminal_id": "VT-015", "pan": "4111111111111111", "merchant_id": "M-1"})
    assert "pan" not in rec and rec["terminal_id"] == "VT-***15"
