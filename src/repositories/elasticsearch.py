"""
Elasticsearch/OpenSearch ödeme olayı deposu (opsiyonel).

Filtreleme ve toplulaştırma sunucuda yapılır (range/term filtreleri, date_histogram, terms, filter,
percentiles, missing). Python'a yalnızca agregasyon sonuçları ve sınırlı sayıda örnek kayıt gelir.
Ham kayıt sayfalaması `search_after` ile yapılır. Her sorguda sunucu tarafı `timeout` ve istemci
tarafı `request_timeout` vardır; reversal bağlantısı MAX_REVERSAL_LINKS ile sınırlandırılır.

Beklenen mapping: timestamp(date); transaction_id, merchant_id, bank_id, terminal_id, payment_method,
transaction_type, transaction_status, response_code, service_name, service_version, channel,
correlation_id (keyword); processing_time_ms (numeric).

Kurulum: pip install -r requirements-es.txt ve .env içinde DATA_SOURCE=elasticsearch.
UYARI: Bu adaptör canlı bir kümede doğrulanmamıştır; sorgu DSL'i ve yanıt ayrıştırma sahte istemciyle test edilir.
"""

import json
from datetime import datetime, timedelta

from src.config import ES_API_KEY, ES_INDEX, ES_TIMEOUT_S, ES_URL, MAX_REVERSAL_LINKS
from src.domain import EVENT_FIELDS, PRIMARY_TYPES, QUALITY_FIELDS
from src.redaction import redact_record
from src.repositories.base import (
    DataSourceUnavailable,
    MalformedResponseError,
    PaymentEventRepository,
    QueryFilter,
)

SERVER_TIMEOUT = f"{int(ES_TIMEOUT_S)}s"
PRIMARY_FILTER = {"terms": {"transaction_type": PRIMARY_TYPES}}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------- Query DSL oluşturucular (saf fonksiyonlar, test edilebilir) ----------


def build_query(f: QueryFilter, *extra: dict, start: datetime | None = None, end: datetime | None = None) -> dict:
    clauses: list[dict] = [{"range": {"timestamp": {"gte": _iso(start or f.start), "lt": _iso(end or f.end)}}}]
    clauses += [{"term": {field: value}} for field, value in f.filters]
    clauses += list(extra)
    return {"bool": {"filter": clauses}}


def stats_aggs() -> dict:
    def status(*values: str) -> dict:
        return {"filter": {"terms": {"transaction_status": list(values)}}}

    return {
        "primary": {
            "filter": PRIMARY_FILTER,
            "aggs": {
                "approved": status("approved"), "declined": status("declined"), "technical": status("error", "timeout"),
                "timeouts": status("timeout"), "errors": status("error"),
                "latency": {
                    "filter": {"bool": {"must_not": [{"term": {"transaction_status": "timeout"}}]}},
                    "aggs": {"avg": {"avg": {"field": "processing_time_ms"}},
                             "pct": {"percentiles": {"field": "processing_time_ms", "percents": [50, 95, 99]}}},
                },
            },
        },
        "reversals": {"filter": {"term": {"transaction_type": "reversal"}}},
        "refunds": {"filter": {"term": {"transaction_type": "refund"}}},
    }


def _body(query: dict, **kw) -> dict:
    return {"size": 0, "timeout": SERVER_TIMEOUT, "query": query, **kw}


def build_summary_body(f: QueryFilter) -> dict:
    return _body(build_query(f), track_total_hits=True, aggs=stats_aggs())


def build_histogram_body(f: QueryFilter, interval_min: int) -> dict:
    return _body(build_query(f), aggs={"buckets": {
        "date_histogram": {"field": "timestamp", "fixed_interval": f"{interval_min}m", "min_doc_count": 0,
                           "extended_bounds": {"min": _iso(f.start), "max": _iso(f.end - timedelta(seconds=1))}},
        "aggs": stats_aggs()}})


def build_group_body(f: QueryFilter, field: str, size: int) -> dict:
    return _body(build_query(f), aggs={"groups": {"terms": {"field": field, "size": size, "order": {"primary": "desc"}},
                                                  "aggs": stats_aggs()}})


def build_terms_body(f: QueryFilter, field: str, size: int) -> dict:
    return _body(build_query(f, PRIMARY_FILTER), aggs={"terms": {"terms": {"field": field, "size": size}}})


def build_sample_body(f: QueryFilter, limit: int, cursor: str | None) -> dict:
    body = {"size": limit, "timeout": SERVER_TIMEOUT, "track_total_hits": True,
            "sort": [{"timestamp": "asc"}, {"transaction_id": "asc"}], "_source": EVENT_FIELDS, "query": build_query(f)}
    if cursor:
        body["search_after"] = json.loads(cursor)
    return body


def build_quality_body(f: QueryFilter) -> dict:
    return _body(build_query(f), track_total_hits=True,
                 aggs={f"missing_{c}": {"missing": {"field": c}} for c in QUALITY_FIELDS})


def build_bounds_body(f: QueryFilter | None) -> dict:
    query = build_query(f) if f else {"match_all": {}}
    return _body(query, aggs={"min_ts": {"min": {"field": "timestamp"}}, "max_ts": {"max": {"field": "timestamp"}}})


def build_docs_body(query: dict, fields: list[str], size: int) -> dict:
    return {"size": size, "timeout": SERVER_TIMEOUT, "track_total_hits": True, "_source": fields, "query": query,
            "sort": [{"timestamp": "asc"}]}


# ---------- Yanıt ayrıştırıcılar ----------


def _round(value) -> float | None:
    return None if value is None else round(float(value), 1)


def parse_stats(node: dict, events: int) -> dict:
    p = node["primary"]
    total = int(p["doc_count"])
    lat = p["latency"]
    pct = (lat.get("pct") or {}).get("values") or {}
    has_latency = int(lat.get("doc_count", 0)) > 0
    return {
        "total": total,
        **{k: int(p[k]["doc_count"]) for k in ("approved", "declined", "technical", "timeouts", "errors")},
        "reversals": int(node["reversals"]["doc_count"]), "refunds": int(node["refunds"]["doc_count"]), "events": events,
        "avg_ms": _round(lat["avg"].get("value")) if has_latency else None,
        "p50_ms": _round(pct.get("50.0")) if has_latency else None,
        "p95_ms": _round(pct.get("95.0")) if has_latency else None,
        "p99_ms": _round(pct.get("99.0")) if has_latency else None,
    }


def parse_summary(resp: dict) -> dict:
    return parse_stats(resp["aggregations"], int(resp["hits"]["total"]["value"]))


def parse_histogram(resp: dict) -> list[dict]:
    return [{"bucket_start": _iso(_parse_ts(b["key_as_string"])), **parse_stats(b, int(b["doc_count"]))}
            for b in resp["aggregations"]["buckets"]["buckets"]]


def parse_groups(resp: dict) -> list[dict]:
    return [{"key": str(b["key"]), **parse_stats(b, int(b["doc_count"]))} for b in resp["aggregations"]["groups"]["buckets"]]


def parse_terms(resp: dict) -> list[dict]:
    return [{"key": str(b["key"]), "count": int(b["doc_count"])} for b in resp["aggregations"]["terms"]["buckets"]]


def classify_reversals(reversals: list[dict], originals: dict[str, dict], auto_max_s: int) -> list[tuple[dict, str]]:
    """Reversal kayıtlarını correlation_id ile bulunan orijinal işleme göre sınıflandırır."""
    out = []
    for r in reversals:
        orig = originals.get(r.get("correlation_id")) if r.get("correlation_id") else None
        if not r.get("correlation_id"):
            category = "unlinkable"
        elif orig is None:
            category = "orphan"
        elif orig["transaction_status"] in ("timeout", "error"):
            category = "after_failure"
        elif orig["transaction_status"] == "approved":
            delay = (_parse_ts(r["timestamp"]) - _parse_ts(orig["timestamp"])).total_seconds()
            category = "auto_after_approval" if delay <= auto_max_s else "void_after_approval"
        else:
            category = "unexpected"
        out.append((r, category))
    return out


def _top(items: list[dict], field: str) -> list[dict]:
    counts: dict[str, int] = {}
    for item in items:
        counts[str(item.get(field))] = counts.get(str(item.get(field)), 0) + 1
    return [{"key": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]


class ElasticsearchPaymentRepository(PaymentEventRepository):
    name = "elasticsearch"

    def __init__(self, client, index: str, max_links: int = MAX_REVERSAL_LINKS):
        self.client = client
        self.index = index
        self.label = index
        self.max_links = max_links

    @classmethod
    def from_config(cls) -> "ElasticsearchPaymentRepository":
        try:
            from elasticsearch import Elasticsearch
        except ImportError as exc:
            raise RuntimeError("Elasticsearch istemcisi kurulu değil: pip install -r requirements-es.txt") from exc
        client = Elasticsearch(ES_URL, api_key=ES_API_KEY or None, request_timeout=ES_TIMEOUT_S, max_retries=1)
        return cls(client, ES_INDEX)

    def _search(self, body: dict, parser=None):
        try:
            resp = dict(self.client.search(index=self.index, **body))
        except Exception as exc:  # bağlantı, kimlik doğrulama, istemci timeout'u
            raise DataSourceUnavailable(f"Elasticsearch sorgusu başarısız: {type(exc).__name__}: {exc}") from exc
        if resp.get("timed_out"):
            raise DataSourceUnavailable("Elasticsearch sorgusu sunucu tarafında zaman aşımına uğradı (kısmi sonuç kullanılmadı).")
        if parser is None:
            return resp
        try:
            return parser(resp)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise MalformedResponseError(f"Beklenmeyen Elasticsearch yanıtı: {type(exc).__name__}: {exc}") from exc

    def _docs(self, query: dict, fields: list[str]) -> tuple[list[dict], bool]:
        resp = self._search(build_docs_body(query, fields, self.max_links),
                            lambda r: ([h["_source"] for h in r["hits"]["hits"]], int(r["hits"]["total"]["value"])))
        docs, total = resp
        return docs, total > len(docs)

    def time_bounds(self) -> tuple[datetime, datetime] | None:
        return self._search(build_bounds_body(None), self._parse_bounds)

    @staticmethod
    def _parse_bounds(resp: dict) -> tuple[datetime, datetime] | None:
        aggs = resp["aggregations"]
        if aggs["min_ts"].get("value") is None:
            return None
        return _parse_ts(aggs["min_ts"]["value_as_string"]), _parse_ts(aggs["max_ts"]["value_as_string"])

    def _summary(self, f: QueryFilter) -> dict:
        return self._search(build_summary_body(f), parse_summary)

    def _histogram(self, f: QueryFilter, interval_min: int) -> list[dict]:
        return self._search(build_histogram_body(f, interval_min), parse_histogram)

    def _group_by(self, f: QueryFilter, field_name: str, size: int) -> list[dict]:
        return self._search(build_group_body(f, field_name, size), parse_groups)

    def _top_terms(self, f: QueryFilter, field_name: str, size: int) -> list[dict]:
        return self._search(build_terms_body(f, field_name, size), parse_terms)

    def _sample(self, f: QueryFilter, limit: int, cursor: str | None) -> dict:
        def parse(resp: dict) -> dict:
            hits = resp["hits"]["hits"]
            total = int(resp["hits"]["total"]["value"])
            more = len(hits) == limit and total > limit
            return {"records": [redact_record(h["_source"]) for h in hits], "total": total,
                    "next_cursor": json.dumps(hits[-1]["sort"]) if more and hits else None}

        try:
            body = build_sample_body(f, limit, cursor)
        except json.JSONDecodeError as exc:
            raise ValueError("Geçersiz sayfalama imleci (cursor).") from exc
        return self._search(body, parse)

    def _reversal_linkage(self, f: QueryFilter, lookback_min: int, auto_reversal_max_s: int) -> dict:
        lookback = timedelta(minutes=lookback_min)
        reversals, trunc_a = self._docs(build_query(f, {"term": {"transaction_type": "reversal"}}),
                                        ["correlation_id", "timestamp", "merchant_id", "bank_id"])
        ids = sorted({r["correlation_id"] for r in reversals if r.get("correlation_id")})
        originals: dict[str, dict] = {}
        trunc_b = False
        if ids:
            unfiltered = QueryFilter(f.start, f.end)
            docs, trunc_b = self._docs(build_query(unfiltered, PRIMARY_FILTER, {"terms": {"correlation_id": ids}},
                                                   start=f.start - lookback),
                                       ["correlation_id", "transaction_status", "timestamp"])
            for d in docs:
                originals.setdefault(d["correlation_id"], d)
        classified = classify_reversals(reversals, originals, auto_reversal_max_s)
        counts = {c: 0 for c in ("void_after_approval", "auto_after_approval", "after_failure", "orphan", "unexpected", "unlinkable")}
        for _, category in classified:
            counts[category] += 1
        technical = [r for r, c in classified if c in ("orphan", "auto_after_approval")]

        cutoff = f.end - timedelta(minutes=2)
        timeouts, trunc_c = self._docs(build_query(f, PRIMARY_FILTER, {"term": {"transaction_status": "timeout"}},
                                                   {"term": {"channel": "POS"}}, end=cutoff), ["correlation_id"])
        timeout_ids = sorted({t["correlation_id"] for t in timeouts if t.get("correlation_id")})
        reversed_ids: set[str] = set()
        trunc_d = False
        if timeout_ids:
            docs, trunc_d = self._docs(build_query(QueryFilter(f.start, f.end), {"term": {"transaction_type": "reversal"}},
                                                   {"terms": {"correlation_id": timeout_ids}}, end=f.end + lookback),
                                       ["correlation_id"])
            reversed_ids = {d["correlation_id"] for d in docs}
        return {
            "total_reversals": len(reversals), "categories": counts,
            "unreversed_timeouts": sum(1 for i in timeout_ids if i not in reversed_ids),
            "technical_by_merchant": _top(technical, "merchant_id"), "technical_by_bank": _top(technical, "bank_id"),
            "truncated": trunc_a or trunc_b or trunc_c or trunc_d, "lookback_min": lookback_min,
        }

    def _field_quality(self, f: QueryFilter) -> dict:
        def parse(resp: dict) -> dict:
            n = int(resp["hits"]["total"]["value"])
            aggs = resp["aggregations"]
            return {"events": n, "null_rates": {c: (round(int(aggs[f"missing_{c}"]["doc_count"]) / n, 4) if n else None)
                                                for c in QUALITY_FIELDS}}

        return self._search(build_quality_body(f), parse)

    def _bounds(self, f: QueryFilter) -> tuple[datetime, datetime] | None:
        return self._search(build_bounds_body(f), self._parse_bounds)
