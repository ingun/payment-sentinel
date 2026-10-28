import pytest

from src.risk_engine import assess_risk, level_for
from tests.conftest import BASELINE, CURRENT


def risk_for(tk, **kwargs):
    return assess_risk(tk.compare_time_windows(CURRENT, BASELINE), tk.analyze_error_patterns(CURRENT, BASELINE), **kwargs)


@pytest.mark.parametrize("scenario,expected", [
    ("normal", {"LOW"}),
    ("error_spike", {"HIGH"}),
    ("latency_degradation", {"MEDIUM", "HIGH"}),
    ("volume_drop", {"MEDIUM", "HIGH"}),
])
def test_risk_levels_per_scenario(make_toolkit, scenario, expected):
    risk = risk_for(make_toolkit(scenario))
    assert risk.level in expected
    assert 0 <= risk.score <= 100


def test_risk_reasons_explain_score(make_toolkit):
    risk = risk_for(make_toolkit("error_spike"))
    assert risk.score == min(100, sum(r.points for r in risk.reasons))
    assert {"Hata oranı (baseline farkı)", "Gecikme artışı", "Anomali süresi"} <= {r.factor for r in risk.reasons}
    assert "demo" in risk.note


def test_insufficient_data_returns_indeterminate(make_toolkit):
    tk = make_toolkit("normal")
    tiny = {"start": "2026-10-27T11:58:00Z", "end": "2026-10-27T12:00:00Z"}
    cmp = tk.compare_time_windows(tiny, BASELINE)
    risk = assess_risk(cmp, tk.analyze_error_patterns(tiny, BASELINE))
    assert risk.level == "INDETERMINATE" and risk.score is None
    assert assess_risk(None, None).level == "INDETERMINATE"


def test_confidence_is_separate_from_risk_score(make_toolkit):
    tk = make_toolkit("error_spike")
    clean = risk_for(tk)
    degraded = risk_for(tk, tool_failures=1, missing_evidence=["search_runbooks"])
    assert clean.score == degraded.score
    assert clean.confidence == "HIGH" and degraded.confidence == "LOW"


def test_level_thresholds():
    assert level_for(0) == "LOW" and level_for(30) == "MEDIUM" and level_for(60) == "HIGH"
