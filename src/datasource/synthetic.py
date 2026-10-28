"""
Sentetik (yerel) veri kaynağı. Demo varsayılanıdır; Elasticsearch adaptörüyle aynı sözleşmeyi uygular.

Zaman sıralı veride pencereyi ikili arama (searchsorted) ile keser, böylece her sorgu
yalnızca ilgili dilimi işler.
"""

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import SCENARIO_DIR
from src.datasource.base import DataSource, QueryFilter

COLUMNS = ["timestamp", "transaction_id", "service", "status", "error_code", "latency_ms", "payment_method", "environment"]


def _to_iso(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _round(value: float | None) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), 1)


def _stats(df: pd.DataFrame) -> dict:
    total = len(df)
    if not total:
        return {"total": 0, "failed": 0, "avg_latency_ms": None, "p95_latency_ms": None}
    latency = df["latency_ms"].to_numpy()
    return {
        "total": total,
        "failed": int(df["is_failed"].sum()),
        "avg_latency_ms": _round(latency.mean()),
        "p95_latency_ms": _round(np.percentile(latency, 95)),
    }


class SyntheticDataSource(DataSource):
    name = "synthetic"

    def __init__(self, df: pd.DataFrame, label: str = "synthetic"):
        df = df.reindex(columns=COLUMNS).copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df["is_failed"] = df["status"] == "failed"
        self.df = df.sort_values("timestamp", kind="stable").reset_index(drop=True)
        self.label = label

    @classmethod
    def from_records(cls, records: list[dict], label: str = "synthetic") -> "SyntheticDataSource":
        return cls(pd.DataFrame(records, columns=COLUMNS), label)

    @classmethod
    def from_jsonl(cls, path: Path) -> "SyntheticDataSource":
        with path.open(encoding="utf-8") as f:
            return cls.from_records([json.loads(line) for line in f if line.strip()], label=path.stem)

    @classmethod
    def from_scenario(cls, scenario: str) -> "SyntheticDataSource":
        path = SCENARIO_DIR / f"{scenario}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"{path} yok. Önce 'python -m src.data_generator' çalıştırın.")
        return cls.from_jsonl(path)

    # ---------- Yardımcılar ----------

    def _slice(self, f: QueryFilter) -> pd.DataFrame:
        ts = self.df["timestamp"]
        lo = ts.searchsorted(pd.Timestamp(f.start), side="left")
        hi = ts.searchsorted(pd.Timestamp(f.end), side="left")
        df = self.df.iloc[lo:hi]
        if f.service:
            df = df[df["service"] == f.service]
        if f.error_code:
            df = df[df["error_code"] == f.error_code]
        if f.status:
            df = df[df["status"] == f.status]
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
        start = pd.Timestamp(f.start).floor(freq)
        index = pd.date_range(start, pd.Timestamp(f.end), freq=freq, inclusive="left")
        groups = dict(tuple(df.groupby(df["timestamp"].dt.floor(freq)))) if len(df) else {}
        empty = df.iloc[0:0]
        return [{"bucket_start": _to_iso(b), **_stats(groups.get(b, empty))} for b in index]

    def _group_by(self, f: QueryFilter, field: str, size: int) -> list[dict]:
        df = self._slice(f)
        rows = [{"key": str(key), **_stats(group)} for key, group in df.groupby(field)]
        rows.sort(key=lambda r: (r["failed"], r["total"]), reverse=True)
        return rows[:size]

    def _top_terms(self, f: QueryFilter, field: str, size: int) -> list[dict]:
        counts = self._slice(f)[field].dropna().value_counts().head(size)
        return [{"key": str(k), "count": int(v)} for k, v in counts.items()]

    def _sample(self, f: QueryFilter, limit: int, offset: int) -> dict:
        df = self._slice(f)
        page = df.iloc[offset : offset + limit][COLUMNS].copy()
        page["timestamp"] = page["timestamp"].map(_to_iso)
        page = page.astype(object).where(page.notna(), None)
        next_offset = offset + limit
        return {
            "records": page.to_dict(orient="records"),
            "total": len(df),
            "next_cursor": next_offset if next_offset < len(df) else None,
        }
