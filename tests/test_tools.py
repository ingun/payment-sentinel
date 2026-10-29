"""Araç sözleşmesi, girdi doğrulama, hata enjeksiyonu ve maskeleme testleri."""

import pytest
from pydantic import ValidationError

from src.repositories import QueryLimitError
from src.tools import ToolError, TransientToolError, build_signature
from src.validation import InputValidationError, validate_tool_output
from tests.conftest import BASELINE, CURRENT, pair


@pytest.mark.parametrize("args, error", [
    (pair() | {"current_window": {"start": "2026-10-27T12:00:00Z", "end": "2026-10-27T11:00:00Z"}}, InputValidationError),
    (pair() | {"current_window": {"start": "2026-10-25T00:00:00Z", "end": "2026-10-27T12:00:00Z"}}, InputValidationError),
    (pair() | {"current_window": {"start": "dün sabah", "end": "2026-10-27T12:00:00Z"}}, InputValidationError),
    (pair({"bank_id": "BANK_B; DROP TABLE"}), ValidationError),
    (pair({"amount": "100"}), ValidationError),
    (pair({"service_name": "unknown-svc"}), ValidationError),
])
def test_invalid_inputs_are_rejected(make_toolkit, args, error):
    with pytest.raises(error):
        make_toolkit().call("compare_with_baseline", args)


def test_breakdown_rejects_unknown_dimension_and_missing_code(make_toolkit):
    tk = make_toolkit()
    with pytest.raises(ValidationError):
        tk.call("breakdown_by_dimension", pair(dimension="amount"))
    with pytest.raises(ValueError):
        tk.call("breakdown_by_dimension", pair(dimension="bank_id", metric="response_code"))


def test_unknown_tool_and_sample_limit(make_toolkit):
    tk = make_toolkit()
    with pytest.raises(ToolError):
        tk.call("refund_transaction", {"transaction_id": "SYN-TX-1"})
    with pytest.raises(ValidationError):
        tk.call("search_transactions", {"window": CURRENT, "limit": 500})


def test_small_models_can_send_window_and_filters_as_text(make_toolkit):
    out = make_toolkit().call("compare_with_baseline", {
        "current_window": f"{CURRENT['start']}/{CURRENT['end']}", "baseline_window": BASELINE,
        "filters": '{"channel": "POS", "bank_id": null}'})
    assert out["filters"] == {"channel": "POS"}


def test_search_transactions_masks_and_pages(make_toolkit):
    tk = make_toolkit()
    out = tk.call("search_transactions", {"window": CURRENT, "filters": {"bank_id": "BANK_B"}, "transaction_status": "timeout", "limit": 5})
    assert out["returned"] == 5 and out["next_cursor"]
    assert all(s["response_code"] == "68" and "***" in s["terminal_id"] for s in out["samples"])
    none = tk.call("search_transactions", {"window": CURRENT, "filters": {"bank_id": "BANK_E"}, "response_code": "68"})
    assert none["total_matches"] == 0 and none["warnings"]


def test_tools_are_read_only(make_toolkit):
    names = set(make_toolkit().registry())
    assert not any(word in n for n in names for word in ("refund", "reverse_", "block", "update", "delete", "write"))


def test_fault_injection(make_toolkit):
    tk = make_toolkit(faults={"analyze_reversals": "timeout_once", "search_runbooks": "error", "check_data_coverage": "malformed"})
    with pytest.raises(TransientToolError):
        tk.call("analyze_reversals", pair())
    assert tk.call("analyze_reversals", pair())["detection"]  # ikinci deneme başarılı
    with pytest.raises(ToolError):
        tk.call("search_runbooks", {"query": "timeout"})
    out = tk.call("check_data_coverage", {"window": CURRENT})
    assert validate_tool_output("check_data_coverage", out)  # şema dışı çıktı yakalanır


def test_signature_is_built_from_tool_outputs_only(make_toolkit):
    tk = make_toolkit("bank_timeout")
    base = tk.call("compare_with_baseline", pair())
    iso = tk.call("breakdown_by_dimension", pair(dimension="bank_id"))
    codes = tk.call("analyze_response_codes", pair({"bank_id": "BANK_B"}))
    sig = build_signature([base, iso, codes])
    assert sig["scope"] == {"bank_id": "BANK_B"}
    assert "68" in sig["response_codes"] and "TIMEOUT_RATE_SPIKE" in sig["detectors"]
    # Araç kendi kayıtlarından imza kurar; LLM'den sayı almaz
    result = tk.call("search_similar_incidents", pair())
    assert result["signature"]["scope"] == {"bank_id": "BANK_B"}


def test_query_limit_error_is_a_value_error():
    assert issubclass(QueryLimitError, ValueError)
