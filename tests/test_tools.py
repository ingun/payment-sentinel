from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.datasource import QueryFilter, QueryLimitError, SyntheticDataSource
from src.retriever import RunbookRetriever
from src.tools import PaymentToolkit, ToolError, TransientToolError
from src.validation import InputValidationError
from tests.conftest import BASELINE, CURRENT


def test_invalid_time_range_is_rejected(make_toolkit):
    tk = make_toolkit()
    with pytest.raises(InputValidationError):
        tk.calculate_payment_metrics("2026-10-27T12:00:00Z", "2026-10-27T11:00:00Z")
    with pytest.raises(InputValidationError):
        tk.calculate_payment_metrics("dün akşam", "2026-10-27T11:00:00Z")
    with pytest.raises(InputValidationError):
        tk.calculate_payment_metrics("2026-10-20T00:00:00Z", "2026-10-27T00:00:00Z")


def test_unknown_error_code_returns_warning(make_toolkit):
    out = make_toolkit().search_payment_logs(CURRENT["start"], CURRENT["end"], error_code="ERR_NOT_EXISTING")
    assert out["total_matches"] == 0
    assert any("katalogda yok" in w for w in out["warnings"])
    assert any("bulunamadı" in w for w in out["warnings"])


def test_unknown_service_is_rejected(make_toolkit):
    with pytest.raises(InputValidationError):
        make_toolkit().calculate_payment_metrics(CURRENT["start"], CURRENT["end"], service="mars-gateway")


def test_empty_log_window(make_toolkit):
    out = make_toolkit().calculate_payment_metrics("2026-10-28T09:00:00Z", "2026-10-28T10:00:00Z")
    assert out["total_attempts"] == 0 and out["error_rate"] is None and out["sufficient_data"] is False


def test_log_search_pagination_and_limit(make_toolkit):
    tk = make_toolkit()
    first = tk.search_payment_logs(CURRENT["start"], CURRENT["end"], status="failed", limit=5)
    second = tk.search_payment_logs(CURRENT["start"], CURRENT["end"], status="failed", limit=5, cursor=first["next_cursor"])
    assert first["returned"] == 5 and first["next_cursor"] == 5
    assert {s["transaction_id"] for s in first["samples"]}.isdisjoint({s["transaction_id"] for s in second["samples"]})
    with pytest.raises(ValidationError):
        tk.call("search_payment_logs", {"start_time": CURRENT["start"], "end_time": CURRENT["end"], "limit": 500})


def test_datasource_guards_limit_query_cost(datasources):
    ds = datasources["normal"]
    start = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    with pytest.raises(QueryLimitError):
        ds.histogram(QueryFilter(start, datetime(2026, 10, 27, 23, 0, tzinfo=timezone.utc)), 1)  # 840 dilim > 500
    with pytest.raises(QueryLimitError):
        ds.group_by(QueryFilter(start, datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)), "transaction_id")
    with pytest.raises(QueryLimitError):
        ds.sample(QueryFilter(start, datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)), limit=1000)


def test_tool_registry_allows_only_known_tools(make_toolkit):
    with pytest.raises(ToolError):
        make_toolkit().call("run_shell", {"cmd": "rm -rf /"})


def test_fault_injection_transient_and_permanent(make_toolkit):
    tk = make_toolkit(faults={"compare_time_windows": "timeout_once", "search_runbooks": "error"})
    with pytest.raises(TransientToolError):
        tk.compare_time_windows(CURRENT, BASELINE)
    assert tk.compare_time_windows(CURRENT, BASELINE)["current"]["total_attempts"] > 0  # ikinci deneme başarılı
    with pytest.raises(ToolError):
        tk.search_runbooks("timeout")


def test_window_can_be_passed_as_string(make_toolkit):
    out = make_toolkit().call("compare_time_windows", {
        "current_window": f"{CURRENT['start']}/{CURRENT['end']}", "baseline_window": BASELINE})
    assert out["current"]["window_start"] == CURRENT["start"]


def test_similar_incidents_signature_is_computed_from_data(make_toolkit):
    out = make_toolkit("error_spike").search_similar_incidents(CURRENT, BASELINE)
    assert out["signature"]["service"] == "provider-connector"
    assert out["matches"][0]["incident_id"] == "INC-2026-0412"
    assert "kanıtlamaz" in out["disclaimer"]


@pytest.mark.parametrize("scenario,expected", [("latency_degradation", "INC-2026-0603"), ("volume_drop", "INC-2026-0805")])
def test_incident_matching_uses_multiple_signals(make_toolkit, scenario, expected):
    out = make_toolkit(scenario).search_similar_incidents(CURRENT, BASELINE)
    assert out["matches"][0]["incident_id"] == expected
    assert set(out["matches"][0]["breakdown"]) >= {"service", "pattern_types", "latency_change", "volume_change"}


def test_no_incident_comparison_without_patterns(make_toolkit):
    out = make_toolkit("normal").search_similar_incidents(CURRENT, BASELINE)
    assert out["found"] is False and out["matches"] == []


def test_empty_datasource():
    tk = PaymentToolkit(SyntheticDataSource.from_records([]), RunbookRetriever(), incidents=[])
    assert tk.ds.time_bounds() is None
    assert tk.calculate_payment_metrics(CURRENT["start"], CURRENT["end"])["total_attempts"] == 0
