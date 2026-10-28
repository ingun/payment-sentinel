"""
Agent'ın çağırabildiği araçlar. Hepsi gerçek Python fonksiyonlarıdır ve salt okunurdur:
hiçbir araç dışarıya yazmaz, ödeme işlemi yapmaz, shell/SQL çalıştırmaz.

Araçlar yalnızca DataSource arayüzünü kullanır; bu sayede sentetik veri ve
Elasticsearch adaptörü aynı araç sözleşmesiyle çalışır.
"""

import json
import logging
import time
from collections.abc import Callable
from datetime import timedelta

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from src.config import BUCKET_MINUTES, KNOWN_ERROR_CODES, SERVICES
from src.datasource import DataSource, QueryFilter
from src.incident_matcher import find_similar, load_incidents, signature_from_analysis
from src.metrics import compare_metrics, metrics_from_summary
from src.patterns import analyze_patterns
from src.retriever import RunbookRetriever
from src.schemas import (
    CompareArgs,
    MetricsArgs,
    PatternArgs,
    ReportArgs,
    RunbookArgs,
    SearchLogsArgs,
    SimilarIncidentArgs,
)
from src.validation import InputValidationError, safe_rate, to_iso, validate_window

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """Kalıcı araç hatası; tekrar denenmez."""


class TransientToolError(ToolError):
    """Geçici araç hatası (ör. timeout); sınırlı sayıda tekrar denenir."""


def _check_service(service: str | None) -> str | None:
    if service in (None, "", "all", "tümü"):
        return None
    if service not in SERVICES:
        raise InputValidationError(f"Bilinmeyen servis: '{service}'. Geçerli servisler: {', '.join(SERVICES)}")
    return service


def _filter(start, end, service=None, error_code=None, status=None) -> QueryFilter:
    start_dt, end_dt = validate_window(start, end)
    return QueryFilter(start_dt, end_dt, service, error_code, status)


class PaymentToolkit:
    """Araçları tek bir veri kaynağına, incident kümesine ve runbook arama motoruna bağlar."""

    def __init__(
        self,
        datasource: DataSource,
        retriever: RunbookRetriever,
        incidents: list[dict] | None = None,
        faults: dict[str, str] | None = None,
    ):
        self.ds = datasource
        self.retriever = retriever
        self.incidents = incidents if incidents is not None else load_incidents()
        # Demo/test için hata enjeksiyonu: {"tool_name": "timeout_once" | "error" | "slow_once"}
        self.faults = dict(faults or {})

    def _maybe_fail(self, name: str) -> None:
        mode = self.faults.get(name)
        if mode == "timeout_once":
            self.faults.pop(name)
            raise TransientToolError(f"{name}: simüle edilmiş zaman aşımı")
        if mode == "slow_once":
            self.faults.pop(name)
            time.sleep(1.0)
        if mode == "error":
            raise ToolError(f"{name}: simüle edilmiş servis hatası")

    def _metrics(self, f: QueryFilter) -> dict:
        return {
            "window_start": to_iso(f.start),
            "window_end": to_iso(f.end),
            "service": f.service,
            **metrics_from_summary(self.ds.summary(f)),
        }

    def _covered_end(self, f: QueryFilter):
        """Pencerenin veriyle kapsanan bitişi; veri sonrasındaki boş dakikalar hacim hesabına girmez."""
        bounds = self.ds.time_bounds()
        if bounds is None:
            return f.end
        data_end = bounds[1].replace(second=0, microsecond=0) + timedelta(minutes=1)
        return max(f.start, min(f.end, data_end))

    def _covered_minutes(self, f: QueryFilter) -> float:
        return (self._covered_end(f) - f.start).total_seconds() / 60

    def _compare(self, cur_f: QueryFilter, base_f: QueryFilter) -> dict:
        current, baseline = self._metrics(cur_f), self._metrics(base_f)
        return {"current": current, "baseline": baseline,
                **compare_metrics(current, baseline, self._covered_minutes(cur_f), self._covered_minutes(base_f))}

    def _buckets(self, f: QueryFilter) -> list[dict]:
        end = to_iso(self._covered_end(f))
        return [b for b in self.ds.histogram(f, BUCKET_MINUTES) if b["bucket_start"] < end]

    def _analysis(self, cur_f: QueryFilter, base_f: QueryFilter) -> tuple[dict, dict]:
        comparison = self._compare(cur_f, base_f)
        cur, base = comparison["current"], comparison["baseline"]
        failed_cur = QueryFilter(cur_f.start, cur_f.end, cur_f.service, status="failed")
        failed_base = QueryFilter(base_f.start, base_f.end, base_f.service, status="failed")
        cur_codes = self.ds.top_terms(failed_cur, "error_code", 5)
        base_codes = self.ds.top_terms(failed_base, "error_code", 20)
        base_by_key = {c["key"]: c["count"] for c in base_codes}
        analysis = analyze_patterns(
            cur=cur,
            base=base,
            cur_buckets=self._buckets(cur_f),
            base_buckets=self._buckets(base_f),
            cur_services=self.ds.group_by(cur_f, "service"),
            base_services=self.ds.group_by(base_f, "service"),
            cur_codes=cur_codes,
            base_codes=base_codes,
            cur_minutes=self._covered_minutes(cur_f),
            base_minutes=self._covered_minutes(base_f),
            interval_min=BUCKET_MINUTES,
        )
        top_codes = [
            {"key": c["key"], "count": c["count"], "share": safe_rate(c["count"], cur["failed"]) or 0.0,
             "baseline_count": base_by_key.get(c["key"], 0)}
            for c in cur_codes
        ]
        result = {
            "window_start": to_iso(cur_f.start),
            "window_end": to_iso(cur_f.end),
            "baseline_start": to_iso(base_f.start),
            "baseline_end": to_iso(base_f.end),
            "service": cur_f.service,
            "total_failed": cur["failed"],
            "top_error_codes": top_codes,
            **analysis,
        }
        return result, comparison

    def _window_pair(self, current_window: dict, baseline_window: dict, service: str | None) -> tuple[QueryFilter, QueryFilter]:
        service = _check_service(service)
        return (
            _filter(current_window["start"], current_window["end"], service),
            _filter(baseline_window["start"], baseline_window["end"], service),
        )

    # ---------- Araçlar ----------

    def search_payment_logs(self, start_time: str, end_time: str, service: str | None = None, error_code: str | None = None,
                            status: str | None = None, limit: int = 10, cursor: int | None = None) -> dict:
        self._maybe_fail("search_payment_logs")
        f = _filter(start_time, end_time, _check_service(service), error_code, status)
        warnings = []
        if error_code and error_code not in KNOWN_ERROR_CODES:
            warnings.append(f"'{error_code}' hata kodu katalogda yok; bilinmeyen kod olarak ele alındı.")
        page = self.ds.sample(f, limit, cursor)
        if page["total"] == 0:
            warnings.append("Bu filtrelerle eşleşen log bulunamadı.")
        return {
            "window_start": to_iso(f.start),
            "window_end": to_iso(f.end),
            "filters": {"service": f.service, "error_code": error_code, "status": status},
            "total_matches": page["total"],
            "returned": len(page["records"]),
            "next_cursor": page["next_cursor"],
            "samples": page["records"],
            "warnings": warnings,
        }

    def calculate_payment_metrics(self, start_time: str, end_time: str, service: str | None = None) -> dict:
        self._maybe_fail("calculate_payment_metrics")
        return self._metrics(_filter(start_time, end_time, _check_service(service)))

    def compare_time_windows(self, current_window: dict, baseline_window: dict, service: str | None = None) -> dict:
        self._maybe_fail("compare_time_windows")
        return self._compare(*self._window_pair(current_window, baseline_window, service))

    def analyze_error_patterns(self, current_window: dict, baseline_window: dict, service: str | None = None) -> dict:
        self._maybe_fail("analyze_error_patterns")
        analysis, _ = self._analysis(*self._window_pair(current_window, baseline_window, service))
        return analysis

    def search_similar_incidents(self, current_window: dict, baseline_window: dict, service: str | None = None) -> dict:
        """İmza veri kaynağından yeniden hesaplanır; LLM'in verdiği sayılara güvenilmez."""
        self._maybe_fail("search_similar_incidents")
        analysis, comparison = self._analysis(*self._window_pair(current_window, baseline_window, service))
        return find_similar(signature_from_analysis(analysis, comparison), self.incidents)

    def search_runbooks(self, query: str) -> dict:
        self._maybe_fail("search_runbooks")
        return self.retriever.search(query)

    def generate_incident_report(self, findings: list[str]) -> dict:
        """Agent'ın bulgu özetini kaydeder. Nihai rapor doğrulamadan sonra report.py ile oluşturulur."""
        self._maybe_fail("generate_incident_report")
        accepted = [f.strip() for f in findings if isinstance(f, str) and f.strip()][:8]
        return {"accepted_findings": accepted, "rejected_findings": []}

    # ---------- Kayıt ----------

    def registry(self) -> dict[str, tuple[Callable, type[BaseModel], str]]:
        return {
            "calculate_payment_metrics": (self.calculate_payment_metrics, MetricsArgs,
                                          "Bir zaman aralığı için toplam/başarılı/başarısız işlem, hata oranı, ortalama ve p95 gecikmeyi hesaplar."),
            "compare_time_windows": (self.compare_time_windows, CompareArgs,
                                     "Güncel dönemi baseline ile karşılaştırır: hata oranı farkı, p95 ve hacim değişimi, fazladan başarısız işlem."),
            "analyze_error_patterns": (self.analyze_error_patterns, PatternArgs,
                                       "Hata sıçraması, hacim anomalisi, hata kodu yoğunlaşması, servis gecikmesi ve hata-gecikme örtüşmesi paternlerini tespit eder."),
            "search_payment_logs": (self.search_payment_logs, SearchLogsArgs,
                                    "Filtrelere uyan log kayıtlarından sayfalı, sınırlı sayıda örnek getirir."),
            "search_similar_incidents": (self.search_similar_incidents, SimilarIncidentArgs,
                                         "Güncel anomali imzasını geçmiş incident kayıtlarıyla karşılaştırır."),
            "search_runbooks": (self.search_runbooks, RunbookArgs, "Operasyon runbook'larında kaynaklı arama yapar."),
            "generate_incident_report": (self.generate_incident_report, ReportArgs,
                                         "Analiz bittiğinde, araç sonuçlarına dayanan bulguları rapora gönderir. En son çağrılır."),
        }

    def call(self, name: str, args: dict) -> dict:
        """İzin verilen araçlardan birini doğrulanmış argümanlarla çalıştırır."""
        registry = self.registry()
        if name not in registry:
            raise ToolError(f"İzin verilmeyen araç: {name}")
        func, schema, _ = registry[name]
        parsed = schema.model_validate(args).model_dump()
        logger.info("tool=%s args=%s", name, json.dumps(parsed, ensure_ascii=False))
        return func(**parsed)

    def as_langchain_tools(self) -> list[StructuredTool]:
        """LLM'e bind_tools ile verilecek şemalar. Çalıştırma agent içindeki executor'da yapılır."""
        return [
            StructuredTool.from_function(func=func, name=name, description=desc, args_schema=schema)
            for name, (func, schema, desc) in self.registry().items()
        ]
