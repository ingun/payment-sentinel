"""
Sentetik (bellek içi) ödeme olayı deposu. Demo varsayılanıdır; Elasticsearch adaptörüyle aynı sözleşmeyi uygular.

Zaman sıralı veride pencereyi ikili arama (searchsorted) ile keser; her sorgu yalnızca ilgili dilimi işler.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from src.domain import EVENT_FIELDS, PRIMARY_TYPES, QUALITY_FIELDS
from src.redaction import redact_record
from src.repositories.base import PaymentEventRepository, QueryFilter


def _to_iso(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _round(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), 1)


def _stats(df: pd.DataFrame) -> dict:
    primary = df[df["is_primary"]]
    total = len(primary)
    status = primary["transaction_status"]
    # Gecikme yüzdelikleri timeout'a düşmeyen işlemler üzerinden; timeout'lar ayrıca sayılır
    latency = primary.loc[status != "timeout", "processing_time_ms"].dropna().to_numpy(dtype=float)
    pct = np.percentile(latency, [50, 95, 99]) if len(latency) else [None, None, None]
    return {
        "total": total,
        "approved": int((status == "approved").sum()),
        "declined": int((status == "declined").sum()),
        "technical": int(status.isin(["error", "timeout"]).sum()),
        "timeouts": int((status == "timeout").sum()),
        "errors": int((status == "error").sum()),
        "reversals": int((df["transaction_type"] == "reversal").sum()),
        "refunds": int((df["transaction_type"] == "refund").sum()),
        "events": len(df),
        "avg_ms": _round(latency.mean()) if len(latency) else None,
        "p50_ms": _round(pct[0]),
        "p95_ms": _round(pct[1]),
        "p99_ms": _round(pct[2]),
    }


class SyntheticPaymentRepository(PaymentEventRepository):
    name = "synthetic"

    def __init__(self, df: pd.DataFrame, label: str = "synthetic"):
        df = df.reindex(columns=EVENT_FIELDS).copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
        df["is_primary"] = df["transaction_type"].isin(PRIMARY_TYPES)
        self.df = df.sort_values("timestamp", kind="stable").reset_index(drop=True)
        self.label = label

    @classmethod
    def from_records(cls, records: list[dict], label: str = "synthetic") -> "SyntheticPaymentRepository":
        return cls(pd.DataFrame(records, columns=EVENT_FIELDS), label)

    @classmethod
    def from_jsonl(cls, path: Path) -> "SyntheticPaymentRepository":
        with path.open(encoding="utf-8") as f:
            return cls.from_records([json.loads(line) for line in f if line.strip()], label=path.stem)

    @classmethod
    def from_scenario(cls, scenario: str, seed: int | None = None) -> "SyntheticPaymentRepository":
        from src.data_generator import SEED, generate_scenario

        return cls.from_records(generate_scenario(scenario, SEED if seed is None else seed), label=scenario)

    # ---------- Yardımcılar ----------

    def _window(self, start: datetime, end: datetime) -> pd.DataFrame:
        ts = self.df["timestamp"]
        lo = ts.searchsorted(pd.Timestamp(start), side="left")
        hi = ts.searchsorted(pd.Timestamp(end), side="left")
        return self.df.iloc[lo:hi]

    def _slice(self, f: QueryFilter) -> pd.DataFrame:
        df = self._window(f.start, f.end)
        for key, value in f.filters:
            df = df[df[key].astype(str) == value]
        return df

    # ---------- Sözleşme ----------

    def time_bounds(self) -> tuple[datetime, datetime] | None:
        if self.df.empty:
            return None
        ts = self.df["timestamp"]
        return ts.iloc[0].to_pydatetime(), ts.iloc[-1].to_pydatetime()

    def _summary(self, f: QueryFilter) -> dict:
        return _stats(self._slice(f))

    def _histogram(self, f: QueryFilter, interval_min: int) -> list[dict]:
        df = self._slice(f)
        freq = f"{interval_min}min"
        index = pd.date_range(pd.Timestamp(f.start).floor(freq), pd.Timestamp(f.end), freq=freq, inclusive="left")
        groups = dict(tuple(df.groupby(df["timestamp"].dt.floor(freq)))) if len(df) else {}
        empty = df.iloc[0:0]
        return [{"bucket_start": _to_iso(b), **_stats(groups.get(b, empty))} for b in index]

    def _group_by(self, f: QueryFilter, field_name: str, size: int) -> list[dict]:
        df = self._slice(f)
        rows = [{"key": str(key), **_stats(group)} for key, group in df.groupby(field_name, dropna=True)]
        rows.sort(key=lambda r: (-r["total"], r["key"]))
        return rows[:size]

    def _top_terms(self, f: QueryFilter, field_name: str, size: int) -> list[dict]:
        df = self._slice(f)
        counts = df[df["is_primary"]][field_name].dropna().astype(str).value_counts()
        items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:size]
        return [{"key": k, "count": int(v)} for k, v in items]

    def _sample(self, f: QueryFilter, limit: int, cursor: str | None) -> dict:
        df = self._slice(f)
        offset = int(cursor) if cursor and str(cursor).isdigit() else 0
        page = df.iloc[offset: offset + limit][EVENT_FIELDS].copy()
        page["timestamp"] = page["timestamp"].map(_to_iso)
        page = page.astype(object).where(page.notna(), None)
        next_offset = offset + limit
        return {
            "records": [redact_record(r) for r in page.to_dict(orient="records")],
            "total": len(df),
            "next_cursor": str(next_offset) if next_offset < len(df) else None,
        }

    def _reversal_linkage(self, f: QueryFilter, lookback_min: int, auto_reversal_max_s: int) -> dict:
        df = self._slice(f)
        reversals = df[df["transaction_type"] == "reversal"]
        lookback = timedelta(minutes=lookback_min)
        context = self._window(f.start - lookback, f.end)
        # correlation_id'si boş kayıtlar bağlanamaz; boş anahtarlar birbirleriyle eşleştirilmez
        originals = context[context["is_primary"] & context["correlation_id"].notna()].drop_duplicates("correlation_id", keep="first")
        merged = reversals.merge(originals[["correlation_id", "transaction_status", "timestamp"]], on="correlation_id",
                                 how="left", suffixes=("", "_orig"))
        delay_s = (merged["timestamp"] - merged["timestamp_orig"]).dt.total_seconds()
        orig = merged["transaction_status_orig"]
        category = np.select(
            [merged["correlation_id"].isna(), orig.isna(), orig.isin(["timeout", "error"]),
             (orig == "approved") & (delay_s <= auto_reversal_max_s), orig == "approved"],
            ["unlinkable", "orphan", "after_failure", "auto_after_approval", "void_after_approval"],
            default="unexpected",
        )
        merged["category"] = category
        technical = merged[merged["category"].isin(["orphan", "auto_after_approval"])]

        # Mutabakat riski: pencere içinde timeout olup reversal kaydı bulunmayan POS işlemleri
        cutoff = pd.Timestamp(f.end) - pd.Timedelta(minutes=2)
        primary = df[df["is_primary"]]
        timeouts = primary[(primary["transaction_status"] == "timeout") & (primary["channel"] == "POS")
                           & (primary["timestamp"] < cutoff) & primary["correlation_id"].notna()]
        later = self._window(f.start, f.end + lookback)
        reversed_ids = set(later.loc[later["transaction_type"] == "reversal", "correlation_id"])
        unreversed = int((~timeouts["correlation_id"].isin(reversed_ids)).sum())

        def top(field_name: str) -> list[dict]:
            counts = technical[field_name].astype(str).value_counts()
            return [{"key": k, "count": int(v)} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]

        counts = pd.Series(category).value_counts()
        return {
            "total_reversals": len(reversals),
            "categories": {c: int(counts.get(c, 0)) for c in
                           ("void_after_approval", "auto_after_approval", "after_failure", "orphan", "unexpected", "unlinkable")},
            "unreversed_timeouts": unreversed,
            "technical_by_merchant": top("merchant_id"),
            "technical_by_bank": top("bank_id"),
            "truncated": False,
            "lookback_min": lookback_min,
        }

    def _field_quality(self, f: QueryFilter) -> dict:
        df = self._slice(f)
        n = len(df)
        return {"events": n, "null_rates": {c: (round(float(df[c].isna().mean()), 4) if n else None) for c in QUALITY_FIELDS}}

    def _bounds(self, f: QueryFilter) -> tuple[datetime, datetime] | None:
        df = self._slice(f)
        if df.empty:
            return None
        return df["timestamp"].iloc[0].to_pydatetime(), df["timestamp"].iloc[-1].to_pydatetime()
