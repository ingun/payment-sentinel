"""Elasticsearch adaptörü: query DSL ve yanıt ayrıştırma testleri (canlı küme gerekmez)."""

from datetime import datetime, timezone

from src.datasource import QueryFilter
from src.datasource.elasticsearch import ElasticsearchDataSource, build_histogram_body, build_query

F = QueryFilter(datetime(2026, 10, 27, 11, 30, tzinfo=timezone.utc), datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc),
                service="provider-connector", status="failed")


class FakeClient:
    def __init__(self, response: dict):
        self.response = response
        self.bodies: list[dict] = []

    def search(self, index: str, **body):
        self.bodies.append({"index": index, **body})
        return self.response


def stats(doc_count: int, failed: int, avg: float, p95: float) -> dict:
    return {"doc_count": doc_count, "failed": {"doc_count": failed}, "avg_latency": {"value": avg},
            "p95_latency": {"values": {"95.0": p95}}}


def test_query_dsl_filters_on_server_side():
    q = build_query(F)["bool"]["filter"]
    assert q[0] == {"range": {"timestamp": {"gte": "2026-10-27T11:30:00Z", "lt": "2026-10-27T12:00:00Z"}}}
    assert {"term": {"service": "provider-connector"}} in q and {"term": {"status": "failed"}} in q
    hist = build_histogram_body(F, 5)
    assert hist["size"] == 0 and hist["aggs"]["buckets"]["date_histogram"]["fixed_interval"] == "5m"


def test_summary_parsing():
    s = stats(368, 54, 751.2, 5235.5)
    client = FakeClient({"hits": {"total": {"value": 368}}, "aggregations": {k: v for k, v in s.items() if k != "doc_count"}})
    out = ElasticsearchDataSource(client, "payment-logs").summary(F)
    assert out == {"total": 368, "failed": 54, "avg_latency_ms": 751.2, "p95_latency_ms": 5235.5}
    assert client.bodies[0]["track_total_hits"] is True and client.bodies[0]["index"] == "payment-logs"


def test_histogram_and_group_parsing():
    bucket = {"key_as_string": "2026-10-27T11:30:00Z", **stats(60, 3, 200.0, 400.0)}
    ds = ElasticsearchDataSource(FakeClient({"aggregations": {"buckets": {"buckets": [bucket]}}}), "i")
    assert ds.histogram(F, 5)[0] == {"bucket_start": "2026-10-27T11:30:00Z", "total": 60, "failed": 3,
                                      "avg_latency_ms": 200.0, "p95_latency_ms": 400.0}
    group = {"key": "provider-connector", **stats(86, 49, 2000.0, 5365.8)}
    ds = ElasticsearchDataSource(FakeClient({"aggregations": {"groups": {"buckets": [group]}}}), "i")
    assert ds.group_by(F, "service")[0]["key"] == "provider-connector"


def test_sample_pagination():
    hits = [{"_source": {"transaction_id": f"TX-{i}"}} for i in range(5)]
    client = FakeClient({"hits": {"total": {"value": 12}, "hits": hits}})
    page = ElasticsearchDataSource(client, "i").sample(F, limit=5, cursor=5)
    assert page["next_cursor"] == 10 and len(page["records"]) == 5
    assert client.bodies[0]["from"] == 5 and client.bodies[0]["size"] == 5
