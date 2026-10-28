import pytest

from src.agent import InvestigationAgent
from src.report import build_report, validate_agent_findings
from src.validation import (
    InputValidationError,
    check_recommendations_supported,
    check_report_consistency,
    check_required_evidence,
    collect_allowed_numbers,
    find_unverified_numbers,
    parse_timestamp,
    validate_tool_output,
    validate_window,
)
from tests.conftest import REQUEST


@pytest.fixture
def finished_state(make_toolkit):
    return InvestigationAgent(make_toolkit("error_spike")).run(REQUEST, thread_id="v")


def test_time_validation():
    assert parse_timestamp("2026-10-27 11:30").isoformat() == "2026-10-27T11:30:00+00:00"
    with pytest.raises(InputValidationError):
        validate_window("2026-10-27T12:00:00Z", "2026-10-27T12:00:00Z")


def test_tool_output_schema_validation():
    assert validate_tool_output("calculate_payment_metrics", {"total_attempts": "çok"})
    assert validate_tool_output("unknown_tool", {})


def test_report_numbers_match_tool_outputs(finished_state):
    report = finished_state["report"]
    compare = next(r["output"] for r in finished_state["results"] if r["tool"] == "compare_time_windows")
    assert report["current"] == compare["current"]
    assert report["comparison"]["error_rate_delta_pp"] == compare["error_rate_delta_pp"]
    assert all(v["passed"] for v in report["validation"])


def test_tampered_report_is_detected(finished_state):
    from src.schemas import IncidentReport

    report = IncidentReport(**finished_state["report"])
    report.current.failed += 7
    report.observations[0].text = "Hata oranı %42.42 oldu."
    issues = check_report_consistency(report, finished_state["results"])
    assert any("uyuşmuyor" in i for i in issues)
    assert any("42.42" in i for i in issues)


def test_recommendations_must_be_supported(finished_state):
    from src.schemas import IncidentReport, Recommendation

    report = IncidentReport(**finished_state["report"])
    assert check_recommendations_supported(report) == []
    report.recommended_steps.append(Recommendation(text="Veritabanını yeniden başlatın", source="-", evidence_refs=["O99"]))
    assert check_recommendations_supported(report)


def test_unverified_numbers_and_causal_claims_are_rejected(finished_state):
    results, scope = finished_state["results"], finished_state["scope"]
    allowed = collect_allowed_numbers(results)
    assert find_unverified_numbers("Hata oranı %14.67, 11:35'te başladı", allowed) == []
    assert find_unverified_numbers("Etkilenen işlem 6543.21", allowed) == ["6543.21"]
    accepted, rejected = validate_agent_findings(
        ["Hata oranı %14.67 oldu.", "Etkilenen işlem 6543.21 oldu.", "Kök neden doğrulandı: sağlayıcı."], results, scope)
    assert accepted == ["Hata oranı %14.67 oldu."]
    assert len(rejected) == 2


def test_negative_tool_values_written_as_decrease_are_accepted():
    allowed = collect_allowed_numbers([{"status": "ok", "output": {"volume_change_pct": -37.5, "p95_change_pct": -5.7}}])
    assert find_unverified_numbers("Dakika başı işlem hacmi %37,5 düşüş gösteriyor.", allowed) == []
    assert find_unverified_numbers("P95 gecikme %5,7 iyileşti.", allowed) == []
    assert find_unverified_numbers("Hacim %42 düştü.", allowed) == ["42"]


def test_required_evidence_detects_missing_tools(finished_state):
    scope = finished_state["scope"]
    assert check_required_evidence(finished_state["results"], scope) == []
    partial = [r for r in finished_state["results"] if r["tool"] != "search_runbooks"]
    assert check_required_evidence(partial, scope) == ["search_runbooks"]
    assert set(check_required_evidence([], scope)) == {"compare_time_windows", "analyze_error_patterns"}


def test_report_without_evidence_is_indeterminate(finished_state):
    report = build_report(scope=finished_state["scope"], results=[], mode="test", data_source="test", assumptions=[],
                          limitations=[], missing=["compare_time_windows"], validation_log=[])
    assert report.risk.level == "INDETERMINATE"
    assert report.confirmed_root_cause is None
    assert any("Eksik kanıt" in x for x in report.limitations)
