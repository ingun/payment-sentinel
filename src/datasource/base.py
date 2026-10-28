"""
Ortak veri kaynağı sözleşmesi ve sorgu sınırları.

Her adaptör aynı şekilde sözlükler döner:
- summary:   {"total", "failed", "avg_latency_ms", "p95_latency_ms"}
- histogram: [{"bucket_start", "total", "failed", "avg_latency_ms", "p95_latency_ms"}]
- group_by:  [{"key", "total", "failed", "avg_latency_ms", "p95_latency_ms"}]
- top_terms: [{"key", "count"}]
- sample:    {"records", "total", "next_cursor"}
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from src.config import MAX_BUCKETS, MAX_SAMPLE_LIMIT, MAX_TERMS, MAX_WINDOW_HOURS

ALLOWED_FIELDS = {"service", "error_code", "status", "payment_method"}


class QueryLimitError(ValueError):
    """Sorgu, güvenli maliyet sınırlarını aşıyor."""


@dataclass(frozen=True)
class QueryFilter:
    start: datetime
    end: datetime
    service: str | None = None
    error_code: str | None = None
    status: str | None = None

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


class DataSource(ABC):
    """Public metotlar sınırları uygular, ardından adaptöre özgü _impl metodunu çağırır."""

    name = "abstract"

    # ---------- Sınır kontrolleri ----------

    @staticmethod
    def _guard_window(f: QueryFilter) -> None:
        if f.start >= f.end:
            raise QueryLimitError("Başlangıç zamanı bitişten önce olmalı.")
        if f.minutes > MAX_WINDOW_HOURS * 60:
            raise QueryLimitError(f"Sorgu penceresi en fazla {MAX_WINDOW_HOURS} saat olabilir.")

    # ---------- Public API ----------

    def summary(self, f: QueryFilter) -> dict:
        self._guard_window(f)
        return self._summary(f)

    def histogram(self, f: QueryFilter, interval_min: int) -> list[dict]:
        self._guard_window(f)
        if interval_min <= 0 or f.minutes / interval_min > MAX_BUCKETS:
            raise QueryLimitError(f"En fazla {MAX_BUCKETS} zaman dilimi sorgulanabilir.")
        return self._histogram(f, interval_min)

    def group_by(self, f: QueryFilter, field: str, size: int = 10) -> list[dict]:
        self._guard_window(f)
        self._guard_field(field)
        return self._group_by(f, field, min(size, MAX_TERMS))

    def top_terms(self, f: QueryFilter, field: str, size: int = 5) -> list[dict]:
        self._guard_window(f)
        self._guard_field(field)
        return self._top_terms(f, field, min(size, MAX_TERMS))

    def sample(self, f: QueryFilter, limit: int = 10, cursor: int | None = None) -> dict:
        self._guard_window(f)
        if limit < 1 or limit > MAX_SAMPLE_LIMIT:
            raise QueryLimitError(f"Örnek kayıt limiti 1-{MAX_SAMPLE_LIMIT} arasında olmalı.")
        return self._sample(f, limit, cursor or 0)

    @staticmethod
    def _guard_field(field: str) -> None:
        if field not in ALLOWED_FIELDS:
            raise QueryLimitError(f"İzin verilmeyen alan: {field}")

    # ---------- Adaptör uygulamaları ----------

    @abstractmethod
    def time_bounds(self) -> tuple[datetime, datetime] | None: ...

    @abstractmethod
    def _summary(self, f: QueryFilter) -> dict: ...

    @abstractmethod
    def _histogram(self, f: QueryFilter, interval_min: int) -> list[dict]: ...

    @abstractmethod
    def _group_by(self, f: QueryFilter, field: str, size: int) -> list[dict]: ...

    @abstractmethod
    def _top_terms(self, f: QueryFilter, field: str, size: int) -> list[dict]: ...

    @abstractmethod
    def _sample(self, f: QueryFilter, limit: int, offset: int) -> dict: ...
