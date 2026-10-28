"""
Elasticsearch/OpenSearch adaptörü (opsiyonel).

Filtreleme ve toplulaştırma sunucuda yapılır (date_histogram, terms, percentiles);
Python'a yalnızca agregasyon sonuçları ve sınırlı sayıda örnek kayıt gelir.

Beklenen mapping: timestamp(date), service/status/error_code/payment_method(keyword), latency_ms(numeric).
Kurulum: pip install -r requirements-es.txt
Not: Bu adaptör canlı bir kümede doğrulanmamıştır; query DSL ve yanıt ayrıştırma birim testlerle kontrol edilir.
"""

from datetime import datetime

from src.config import ES_API_KEY, ES_INDEX, ES_URL
from src.datasource.base import DataSource, QueryFilter
from src.datasource.synthetic import COLUMNS


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- Query DSL oluşturucular (saf fonksiyonlar, test edilebilir) ----------


def build_query(f: QueryFilter) -> dict:
    clauses: list[dict] = [{"range": {"timestamp": {"gte": _iso(f.start), "lt": _iso(f.end)}}}]
    for field in ("service", "error_code", "status"):
        value = getattr(f, field)
        if value:
            clauses.append({"term": {field: value}})
    return {"bool": {"filter": clauses}}


def stats_aggs() -> dict:
    return {
        "failed": {"filter": {"term": {"status": "failed"}}},
        "avg_latency": {"avg": {"field": "latency_ms"}},
        "p95_latency": {"percentiles": {"field": "latency_ms", "percents": [95]}},
    }


def build_summary_body(f: QueryFilter) -> dict:
    return {"size": 0, "track_total_hits": True, "query": build_query(f), "aggs": stats_aggs()}


def build_histogram_body(f: QueryFilter, interval_min: int) -> dict:
    return {
        "size": 0,
        "query": build_query(f),
        "aggs": {
            "buckets": {
                "date_histogram": {
                    "field": "timestamp",
                    "fixed_interval": f"{interval_min}m",
                    "min_doc_count": 0,
                    "extended_bounds": {"min": _iso(f.start), "max": _iso(f.end)},
                },
                "aggs": stats_aggs(),
            }
        },
    }


def build_group_body(f: QueryFilter, field: str, size: int) -> dict:
    return {
        "size": 0,
        "query": build_query(f),
        "aggs": {"groups": {"terms": {"field": field, "size": size, "order": {"failed": "desc"}}, "aggs": stats_aggs()}},
    }


def build_terms_body(f: QueryFilter, field: str, size: int) -> dict:
    return {"size": 0, "query": build_query(f), "aggs": {"terms": {"terms": {"field": field, "size": size}}}}


def build_sample_body(f: QueryFilter, limit: int, offset: int) -> dict:
    return {
        "size": limit,
        "from": offset,
        "track_total_hits": True,
        "sort": [{"timestamp": "asc"}],
        "_source": COLUMNS,
        "query": build_query(f),
    }


# ---------- Yanıt ayrıştırıcılar ----------


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


def parse_stats(node: dict, total: int) -> dict:
    p95 = (node.get("p95_latency") or {}).get("values", {}).get("95.0")
    return {
        "total": total,
        "failed": int(node.get("failed", {}).get("doc_count", 0)),
        "avg_latency_ms": _round(node.get("avg_latency", {}).get("value")) if total else None,
        "p95_latency_ms": _round(p95) if total else None,
    }


def parse_summary(resp: dict) -> dict:
    return parse_stats(resp.get("aggregations", {}), int(resp["hits"]["total"]["value"]))


def parse_histogram(resp: dict) -> list[dict]:
    buckets = resp["aggregations"]["buckets"]["buckets"]
    return [{"bucket_start": b["key_as_string"], **parse_stats(b, int(b["doc_count"]))} for b in buckets]


def parse_groups(resp: dict) -> list[dict]:
    return [{"key": str(b["key"]), **parse_stats(b, int(b["doc_count"]))} for b in resp["aggregations"]["groups"]["buckets"]]


def parse_terms(resp: dict) -> list[dict]:
    return [{"key": str(b["key"]), "count": int(b["doc_count"])} for b in resp["aggregations"]["terms"]["buckets"]]


class ElasticsearchDataSource(DataSource):
    name = "elasticsearch"

    def __init__(self, client, index: str):
        self.client = client
        self.index = index

    @classmethod
    def from_config(cls) -> "ElasticsearchDataSource":
        try:
            from elasticsearch import Elasticsearch
        except ImportError as exc:
            raise RuntimeError("Elasticsearch istemcisi kurulu değil: pip install -r requirements-es.txt") from exc
        client = Elasticsearch(ES_URL, api_key=ES_API_KEY or None, request_timeout=10)
        return cls(client, ES_INDEX)

    def _search(self, body: dict) -> dict:
        resp = self.client.search(index=self.index, **body)
        return dict(resp)

    def time_bounds(self) -> tuple[datetime, datetime] | None:
        resp = self._search(
            {"size": 0, "aggs": {"min_ts": {"min": {"field": "timestamp"}}, "max_ts": {"max": {"field": "timestamp"}}}}
        )
        aggs = resp["aggregations"]
        if aggs["min_ts"].get("value") is None:
            return None
        parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
        return parse(aggs["min_ts"]["value_as_string"]), parse(aggs["max_ts"]["value_as_string"])

    def _summary(self, f: QueryFilter) -> dict:
        return parse_summary(self._search(build_summary_body(f)))

    def _histogram(self, f: QueryFilter, interval_min: int) -> list[dict]:
        return parse_histogram(self._search(build_histogram_body(f, interval_min)))

    def _group_by(self, f: QueryFilter, field: str, size: int) -> list[dict]:
        return parse_groups(self._search(build_group_body(f, field, size)))

    def _top_terms(self, f: QueryFilter, field: str, size: int) -> list[dict]:
        return parse_terms(self._search(build_terms_body(f, field, size)))

    def _sample(self, f: QueryFilter, limit: int, offset: int) -> dict:
        resp = self._search(build_sample_body(f, limit, offset))
        hits = resp["hits"]["hits"]
        total = int(resp["hits"]["total"]["value"])
        next_offset = offset + limit
        return {
            "records": [h["_source"] for h in hits],
            "total": total,
            "next_cursor": next_offset if next_offset < total else None,
        }
