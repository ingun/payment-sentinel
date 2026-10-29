"""Sentetik veri üretici ve depo (repository) testleri."""

from datetime import datetime, timedelta, timezone

import pytest

from src.data_generator import SCENARIO_LABELS, SCENARIOS, generate_scenario
from src.domain import EVENT_FIELDS, RESPONSE_CODES, status_for
from src.repositories import QueryFilter, QueryLimitError, SyntheticPaymentRepository
from src.validation import parse_timestamp

T = parse_timestamp


def window(start: str = "2026-10-27T11:00:00Z", end: str = "2026-10-27T12:00:00Z", **filters) -> QueryFilter:
    return QueryFilter.make(T(start), T(end), filters)


def test_generation_is_deterministic_and_seed_dependent():
    a, b = generate_scenario("bank_timeout"), generate_scenario("bank_timeout")
    assert a == b
    assert generate_scenario("bank_timeout", seed=7) != a


def test_every_scenario_has_labels_and_valid_schema():
    assert set(SCENARIO_LABELS) == set(SCENARIOS)
    for scenario in SCENARIOS:
        events = generate_scenario(scenario)
        assert events, scenario
        sample = events[len(events) // 2]
        assert set(sample) == set(EVENT_FIELDS)
        assert all(e["data_label"] == "synthetic" for e in events)
        assert all(e["response_code"] in RESPONSE_CODES for e in events)
        assert all(status_for(e["response_code"]) == e["transaction_status"] for e in events)


def test_data_covers_today_and_same_window_yesterday():
    ts = [e["timestamp"] for e in generate_scenario("normal_baseline")]
    assert any(t.startswith("2026-10-26T06") for t in ts) and any(t.startswith("2026-10-27T11") for t in ts)


def test_reversals_share_correlation_id_with_original():
    events = generate_scenario("bank_timeout")
    primary = {e["correlation_id"]: e for e in events if e["transaction_type"] in ("authorization", "sale")}
    reversals = [e for e in events if e["transaction_type"] == "reversal"]
    linked = [r for r in reversals if r["correlation_id"] in primary]
    assert linked and len(linked) < len(reversals) + 1
    assert all(primary[r["correlation_id"]]["timestamp"] < r["timestamp"] for r in linked)
    # Timeout'a düşen POS işlemlerinin çoğu koruyucu reversal alır
    assert any(primary[r["correlation_id"]]["transaction_status"] == "timeout" for r in linked)


def test_query_filter_rejects_unknown_field_and_unsafe_value():
    with pytest.raises(QueryLimitError):
        window(amount="100")
    with pytest.raises(QueryLimitError):
        window(bank_id="BANK_B' OR 1=1 --")
    assert window(bank_id="BANK_B", channel=None).as_dict() == {"bank_id": "BANK_B"}


def test_repository_guards(repos):
    repo = repos["normal_baseline"]
    with pytest.raises(QueryLimitError):
        repo.summary(window("2026-10-27T12:00:00Z", "2026-10-27T11:00:00Z"))
    with pytest.raises(QueryLimitError):
        repo.summary(QueryFilter(T("2026-10-25T00:00:00Z"), T("2026-10-27T00:00:00Z")))
    with pytest.raises(QueryLimitError):
        repo.sample(window(), limit=51)
    with pytest.raises(QueryLimitError):
        repo.group_by(window(), "amount")


def test_summary_counts_match_independent_calculation(repos):
    f = window()
    events = [e for e in generate_scenario("bank_timeout") if "2026-10-27T11:00:00" <= e["timestamp"] < "2026-10-27T12:00:00"]
    primary = [e for e in events if e["transaction_type"] in ("authorization", "sale")]
    s = repos["bank_timeout"].summary(f)
    assert s["total"] == len(primary)
    assert s["timeouts"] == sum(e["transaction_status"] == "timeout" for e in primary)
    assert s["technical"] == sum(e["transaction_status"] in ("error", "timeout") for e in primary)
    assert s["reversals"] == sum(e["transaction_type"] == "reversal" for e in events)
    # Gecikme yüzdelikleri timeout'ları içermez (timeout süresi ~10 sn)
    assert s["p99_ms"] < 10_000


def test_sample_is_paginated_bounded_and_redacted(repos):
    repo = repos["bank_timeout"]
    f = window(bank_id="BANK_B", transaction_status="timeout")
    first = repo.sample(f, limit=3)
    second = repo.sample(f, limit=3, cursor=first["next_cursor"])
    assert first["total"] == second["total"] > 3
    assert {r["transaction_id"] for r in first["records"]}.isdisjoint(r["transaction_id"] for r in second["records"])
    assert all("***" in r["terminal_id"] for r in first["records"])


def test_reversal_linkage_classifies_categories(repos):
    link = repos["reversal_anomaly"].reversal_linkage(window(), 30, 60)
    cats = link["categories"]
    assert sum(cats.values()) == link["total_reversals"]
    assert cats["auto_after_approval"] > cats["void_after_approval"]
    assert link["technical_by_merchant"][0]["key"] == "M-007"
    timeouts = repos["bank_timeout"].reversal_linkage(window(), 30, 60)
    assert timeouts["categories"]["after_failure"] > 10  # timeout'ların koruyucu reversal'ları ayrı sınıflanır


def test_field_quality_and_bounds(repos):
    repo = repos["version_regression"]
    q = repo.field_quality(window())
    assert q["events"] > 0 and all(v == 0 for v in q["null_rates"].values())
    first, _ = repo.bounds(QueryFilter.make(T("2026-10-27T06:00:00Z"), T("2026-10-27T12:00:00Z"), {"service_version": "2.4.0"}))
    assert first >= datetime(2026, 10, 27, 10, 45, tzinfo=timezone.utc)
    assert first < datetime(2026, 10, 27, 10, 45, tzinfo=timezone.utc) + timedelta(minutes=5)


def test_empty_repository_is_safe():
    repo = SyntheticPaymentRepository.from_records([])
    assert repo.time_bounds() is None
    s = repo.summary(window())
    assert s["total"] == 0 and s["p95_ms"] is None
