"""
Veri erişim sözleşmeleri: PaymentEventRepository, HistoricalIncidentRepository, RunbookRepository.

Ödeme olayı deposu her zaman toplulaştırılmış sonuç döner; ham kayıt yalnızca `sample` ile,
sayfalı ve en fazla MAX_SAMPLE_LIMIT kadar gelir. Oran ve gecikme istatistikleri birincil
ödeme işlemleri (authorization + sale) üzerinden hesaplanır; reversal/refund ayrı sayılır.
Gecikme yüzdelikleri timeout'a düşmeyen birincil işlemlerden hesaplanır (timeout'lar `timeouts` alanında).

İstatistik sözlüğü (summary, histogram, group_by satırları):
    total, approved, declined, technical, timeouts, errors, reversals, refunds, events,
    avg_ms, p50_ms, p95_ms, p99_ms
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.config import MAX_BUCKETS, MAX_SAMPLE_LIMIT, MAX_TERMS, MAX_WINDOW_HOURS
from src.domain import FILTER_FIELDS

VALUE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class QueryLimitError(ValueError):
    """Sorgu, güvenli maliyet sınırlarını aşıyor veya izin verilmeyen bir alan içeriyor."""


class DataSourceError(RuntimeError):
    """Veri kaynağı hatası (araç tarafında kalıcı hata olarak ele alınır)."""


class DataSourceUnavailable(DataSourceError):
    """Veri kaynağına ulaşılamıyor."""


class MalformedResponseError(DataSourceError):
    """Veri kaynağından beklenmeyen biçimde yanıt geldi."""


@dataclass(frozen=True)
class QueryFilter:
    start: datetime
    end: datetime
    filters: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @classmethod
    def make(cls, start: datetime, end: datetime, filters: dict | None = None) -> "QueryFilter":
        clean: dict[str, str] = {}
        for key, value in (filters or {}).items():
            if value in (None, ""):
                continue
            if key not in FILTER_FIELDS:
                raise QueryLimitError(f"İzin verilmeyen filtre alanı: {key}")
            value = str(value)
            if not VALUE_RE.match(value):
                raise QueryLimitError(f"Geçersiz filtre değeri: {key}={value!r}")
            clean[key] = value
        return cls(start, end, tuple(sorted(clean.items())))

    def get(self, key: str) -> str | None:
        return dict(self.filters).get(key)

    def as_dict(self) -> dict[str, str]:
        return dict(self.filters)

    def with_filters(self, **changes: str | None) -> "QueryFilter":
        merged = {**self.as_dict(), **changes}
        return QueryFilter.make(self.start, self.end, merged)

    def shifted(self, delta: timedelta) -> "QueryFilter":
        return QueryFilter(self.start + delta, self.end + delta, self.filters)

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


class PaymentEventRepository(ABC):
    """Public metotlar sınırları uygular, ardından adaptöre özgü _impl metodunu çağırır."""

    name = "abstract"
    label = ""

    @staticmethod
    def _guard_window(f: QueryFilter) -> None:
        if f.start >= f.end:
            raise QueryLimitError("Başlangıç zamanı bitişten önce olmalı.")
        if f.minutes > MAX_WINDOW_HOURS * 60:
            raise QueryLimitError(f"Sorgu penceresi en fazla {MAX_WINDOW_HOURS} saat olabilir.")

    @staticmethod
    def _guard_field(field_name: str) -> None:
        if field_name not in FILTER_FIELDS:
            raise QueryLimitError(f"İzin verilmeyen alan: {field_name}")

    # ---------- Public API ----------

    def summary(self, f: QueryFilter) -> dict:
        self._guard_window(f)
        return self._summary(f)

    def histogram(self, f: QueryFilter, interval_min: int) -> list[dict]:
        self._guard_window(f)
        if interval_min <= 0 or f.minutes / interval_min > MAX_BUCKETS:
            raise QueryLimitError(f"En fazla {MAX_BUCKETS} zaman dilimi sorgulanabilir.")
        return self._histogram(f, interval_min)

    def group_by(self, f: QueryFilter, field_name: str, size: int = 10) -> list[dict]:
        self._guard_window(f)
        self._guard_field(field_name)
        return self._group_by(f, field_name, max(1, min(size, MAX_TERMS)))

    def top_terms(self, f: QueryFilter, field_name: str, size: int = 5) -> list[dict]:
        self._guard_window(f)
        self._guard_field(field_name)
        return self._top_terms(f, field_name, max(1, min(size, MAX_TERMS)))

    def sample(self, f: QueryFilter, limit: int = 10, cursor: str | None = None) -> dict:
        self._guard_window(f)
        if limit < 1 or limit > MAX_SAMPLE_LIMIT:
            raise QueryLimitError(f"Örnek kayıt limiti 1-{MAX_SAMPLE_LIMIT} arasında olmalı.")
        return self._sample(f, limit, cursor)

    def reversal_linkage(self, f: QueryFilter, lookback_min: int, auto_reversal_max_s: int) -> dict:
        """Penceredeki reversal'ları correlation_id ile orijinal işleme bağlayıp sınıflandırır.

        Dönüş: total_reversals, categories{void_after_approval, auto_after_approval, after_failure,
        orphan, unexpected, unlinkable (correlation_id boş)}, unreversed_timeouts, technical_by_merchant, technical_by_bank, truncated.
        """
        self._guard_window(f)
        return self._reversal_linkage(f, lookback_min, auto_reversal_max_s)

    def field_quality(self, f: QueryFilter) -> dict:
        """{"events": n, "null_rates": {alan: oran}}"""
        self._guard_window(f)
        return self._field_quality(f)

    def bounds(self, f: QueryFilter) -> tuple[datetime, datetime] | None:
        """Filtreye uyan ilk ve son olay zamanı (ör. bir sürümün ilk görüldüğü an)."""
        self._guard_window(f)
        return self._bounds(f)

    # ---------- Adaptör uygulamaları ----------

    @abstractmethod
    def time_bounds(self) -> tuple[datetime, datetime] | None: ...

    @abstractmethod
    def _summary(self, f: QueryFilter) -> dict: ...

    @abstractmethod
    def _histogram(self, f: QueryFilter, interval_min: int) -> list[dict]: ...

    @abstractmethod
    def _group_by(self, f: QueryFilter, field_name: str, size: int) -> list[dict]: ...

    @abstractmethod
    def _top_terms(self, f: QueryFilter, field_name: str, size: int) -> list[dict]: ...

    @abstractmethod
    def _sample(self, f: QueryFilter, limit: int, cursor: str | None) -> dict: ...

    @abstractmethod
    def _reversal_linkage(self, f: QueryFilter, lookback_min: int, auto_reversal_max_s: int) -> dict: ...

    @abstractmethod
    def _field_quality(self, f: QueryFilter) -> dict: ...

    @abstractmethod
    def _bounds(self, f: QueryFilter) -> tuple[datetime, datetime] | None: ...


class HistoricalIncidentRepository(ABC):
    @abstractmethod
    def all(self) -> list[dict]: ...


class RunbookRepository(ABC):
    @abstractmethod
    def documents(self) -> list[tuple[str, str]]:
        """[(kaynak dosya adı, Markdown metni)]"""
