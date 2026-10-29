"""Elasticsearch adaptörü: query DSL üretimi, yanıt ayrıştırma ve hata yönetimi (sahte istemci; canlı küme yok)."""

import json

import pytest

from src.repositories import DataSourceUnavailable, MalformedResponseError, QueryFilter
from src.repositories.elasticsearch import (
    ElasticsearchPaymentRepository,
    build_group_body,
    build_histogram_body,
    build_sample_body,
    build_summary_body,
    classify_reversals,
    parse_summary,
)
from src.validation import parse_timestamp

F = QueryFilter.make(parse_timestamp("2026-10-27T11:00:00Z"), parse_timestamp("2026-10-27T12:00:00Z"), {"bank_id": "BANK_B"})


def stats_node(total=100, technical=7, timeouts=5, reversals=3, events=None):
    def c(n):
        return {"doc_count": n}

    return {
        "doc_count": events or total + reversals,
        "primary": {"doc_count": total, "approved": c(total - technical - 4), "declined": c(4), "technical": c(technical),
                    "timeouts": c(timeouts), "errors": c(technical - timeouts),
                    "latency": {"doc_count": total - timeouts, "avg": {"value": 410.26},
                                "pct": {"values": {"50.0": 350.0, "95.0": 900.44, "99.0": 1500.0}}}},
        "reversals": c(reversals), "refunds": c(1),
    }


class FakeClient:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.bodies = []

    def search(self, index, **body):
        self.bodies.append(body)
        if self.error:
            raise self.error
        return self.responses.pop(0)


def test_query_dsl_filters_and_limits():
    body = build_summary_body(F)
    clauses = body["query"]["bool"]["filter"]
    assert clauses[0] == {"range": {"timestamp": {"gte": "2026-10-27T11:00:00Z", "lt": "2026-10-27T12:00:00Z"}}}
    assert {"term": {"bank_id": "BANK_B"}} in clauses
    assert body["size"] == 0 and body["timeout"]
    assert body["aggs"]["primary"]["filter"] == {"terms": {"transaction_type": ["authorization", "sale"]}}
    hist = build_histogram_body(F, 5)["aggs"]["buckets"]["date_histogram"]
    assert hist["fixed_interval"] == "5m" and hist["min_doc_count"] == 0
    group = build_group_body(F, "merchant_id", 20)["aggs"]["groups"]["terms"]
    assert group == {"field": "merchant_id", "size": 20, "order": {"primary": "desc"}}


def test_sample_uses_search_after_pagination():
    body = build_sample_body(F, 10, json.dumps(["2026-10-27T11:05:00Z", "SYN-TX-1"]))
    assert body["search_after"] == ["2026-10-27T11:05:00Z", "SYN-TX-1"] and "from" not in body
    assert body["size"] == 10


def test_parse_summary():
    s = parse_summary({"hits": {"total": {"value": 103}}, "aggregations": stats_node()})
    assert s["total"] == 100 and s["technical"] == 7 and s["timeouts"] == 5 and s["reversals"] == 3
    assert s["p95_ms"] == 900.4 and s["events"] == 103


def test_repository_summary_and_sample_through_fake_client():
    hit = {"_source": {"transaction_id": "SYN-TX-1", "terminal_id": "T-007-3", "bank_id": "BANK_B"}, "sort": ["a", "b"]}
    client = FakeClient([
        {"hits": {"total": {"value": 103}}, "aggregations": stats_node()},
        {"hits": {"total": {"value": 5}, "hits": [hit, hit]}},
    ])
    repo = ElasticsearchPaymentRepository(client, "payments")
    assert repo.summary(F)["total"] == 100
    page = repo.sample(F, limit=2)
    assert page["records"][0]["terminal_id"] == "T-***-3"
    assert json.loads(page["next_cursor"]) == ["a", "b"]


def test_unavailable_and_malformed_responses():
    down = ElasticsearchPaymentRepository(FakeClient(error=ConnectionError("refused")), "payments")
    with pytest.raises(DataSourceUnavailable):
        down.summary(F)
    timed_out = ElasticsearchPaymentRepository(FakeClient([{"timed_out": True}]), "payments")
    with pytest.raises(DataSourceUnavailable):
        timed_out.summary(F)
    bad = ElasticsearchPaymentRepository(FakeClient([{"hits": {}, "aggregations": {}}]), "payments")
    with pytest.raises(MalformedResponseError):
        bad.summary(F)


def test_reversal_classification_and_bounded_linkage():
    originals = {"c1": {"transaction_status": "approved", "timestamp": "2026-10-27T11:00:00Z"},
                 "c2": {"transaction_status": "approved", "timestamp": "2026-10-27T11:00:00Z"},
                 "c3": {"transaction_status": "timeout", "timestamp": "2026-10-27T11:00:00Z"}}
    reversals = [{"correlation_id": c, "timestamp": ts} for c, ts in
                 [("c1", "2026-10-27T11:00:10Z"), ("c2", "2026-10-27T11:10:00Z"), ("c3", "2026-10-27T11:00:30Z"), ("c4", "2026-10-27T11:01:00Z")]]
    cats = [c for _, c in classify_reversals(reversals, originals, 60)]
    assert cats == ["auto_after_approval", "void_after_approval", "after_failure", "orphan"]

    def docs(items, total=None):
        return {"hits": {"total": {"value": total or len(items)}, "hits": [{"_source": i} for i in items]}}

    client = FakeClient([
        docs(reversals, total=9000),  # üst sınır aşıldı → truncated
        docs([{"correlation_id": k, **v} for k, v in originals.items()]),
        docs([{"correlation_id": "c9"}]),
        docs([]),
    ])
    link = ElasticsearchPaymentRepository(client, "payments", max_links=4).reversal_linkage(F, 30, 60)
    assert link["categories"]["orphan"] == 1 and link["unreversed_timeouts"] == 1 and link["truncated"]
    assert all(b["size"] <= 4 for b in client.bodies)


def test_histogram_groups_terms_quality_and_bounds_through_fake_client():
    bucket = {"key_as_string": "2026-10-27T11:00:00.000Z", **stats_node(total=50, events=52)}
    group = {"key": "BANK_B", **stats_node(total=40, events=41)}
    client = FakeClient([
        {"aggregations": {"buckets": {"buckets": [bucket]}}},
        {"aggregations": {"groups": {"buckets": [group]}}},
        {"aggregations": {"terms": {"buckets": [{"key": "68", "doc_count": 9}]}}},
        {"hits": {"total": {"value": 100}}, "aggregations": {f"missing_{c}": {"doc_count": 5} for c in
                                                             ("bank_id", "merchant_id", "response_code", "processing_time_ms",
                                                              "service_name", "service_version", "correlation_id")}},
        {"aggregations": {"min_ts": {"value": 1, "value_as_string": "2026-10-27T06:00:00Z"},
                          "max_ts": {"value": 2, "value_as_string": "2026-10-27T12:00:00Z"}}},
        {"aggregations": {"min_ts": {"value": None}, "max_ts": {"value": None}}},
    ])
    repo = ElasticsearchPaymentRepository(client, "payments")
    hist = repo.histogram(F, 5)
    assert hist[0]["bucket_start"] == "2026-10-27T11:00:00Z" and hist[0]["total"] == 50
    assert repo.group_by(F, "bank_id")[0]["key"] == "BANK_B"
    assert repo.top_terms(F, "response_code") == [{"key": "68", "count": 9}]
    quality = repo.field_quality(F)
    assert quality["events"] == 100 and quality["null_rates"]["correlation_id"] == 0.05
    first, last = repo.time_bounds()
    assert first.hour == 6 and last.hour == 12
    assert repo.bounds(F) is None
    # Terim agregasyonu yalnızca birincil işlemleri sayar; sorgu sunucu tarafı timeout taşır
    assert {"terms": {"transaction_type": ["authorization", "sale"]}} in client.bodies[2]["query"]["bool"]["filter"]
    assert all(b.get("timeout") for b in client.bodies)


def test_invalid_pagination_cursor_is_rejected():
    repo = ElasticsearchPaymentRepository(FakeClient([]), "payments")
    with pytest.raises(ValueError, match="imleci"):
        repo.sample(F, limit=5, cursor="not-json")


def test_unlinkable_reversals_in_es_classification():
    cats = [c for _, c in classify_reversals([{"correlation_id": None, "timestamp": "2026-10-27T11:00:00Z"}], {}, 60)]
    assert cats == ["unlinkable"]
