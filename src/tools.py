"""
Agent'ın çağırabildiği araçlar. Hepsi gerçek Python fonksiyonlarıdır ve SALT OKUNURDUR:
hiçbir araç veri yazmaz, ödeme/reversal/iade yapmaz, işlem bloklamaz, shell/SQL çalıştırmaz.

Araçlar yalnızca depo arayüzlerini kullanır; böylece sentetik depo ve Elasticsearch adaptörü
aynı araç sözleşmesiyle çalışır. Tüm sayılar deterministik dedektörlerden gelir.
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import timedelta

from pydantic import BaseModel

from src.analytics import detectors as D
from src.analytics.quality import assess_evidence
from src.analytics.thresholds import Thresholds, default_thresholds
from src.config import BUCKET_MINUTES, REVERSAL_LOOKBACK_MIN
from src.domain import QUALITY_FIELDS
from src.incident_matcher import find_similar
from src.metrics import excess_count, pct_change, rate_delta_pp, with_rates
from src.repositories import (
    HistoricalIncidentRepository,
    InMemoryIncidentRepository,
    JsonIncidentRepository,
    PaymentEventRepository,
    QueryFilter,
)
from src.retriever import RunbookRetriever
from src.schemas import (
    BaselineArgs,
    BreakdownArgs,
    FindingsArgs,
    ResponseCodeArgs,
    RunbookArgs,
    TransactionSearchArgs,
    VersionArgs,
    WindowArgs,
)
from src.validation import parse_timestamp, to_iso, validate_window

logger = logging.getLogger(__name__)

COUNT_KEYS = {"technical_rate": "technical", "decline_rate": "declined", "timeout_rate": "timeouts",
              "reversal_ratio": "reversals", "response_code": "code"}


class ToolError(Exception):
    """Kalıcı araç hatası; tekrar denenmez."""


class TransientToolError(ToolError):
    """Geçici araç hatası (ör. timeout); sınırlı sayıda tekrar denenir."""


def _win(window: dict) -> dict:
    start, end = validate_window(window["start"], window["end"])
    return {"start": to_iso(start), "end": to_iso(end)}


class PaymentToolkit:
    """Araçları bir ödeme olayı deposuna, incident deposuna ve runbook arama motoruna bağlar."""

    def __init__(self, repository: PaymentEventRepository, retriever: RunbookRetriever,
                 incidents: HistoricalIncidentRepository | list[dict] | None = None,
                 faults: dict[str, str] | None = None, thresholds: Thresholds | None = None):
        self.repo = repository
        self.retriever = retriever
        if isinstance(incidents, list):
            incidents = InMemoryIncidentRepository(incidents)
        self.incidents = incidents or JsonIncidentRepository()
        self.th = (thresholds or default_thresholds()).detection
        # Demo/test için hata enjeksiyonu: {"tool_name": "timeout_once" | "error" | "slow_once" | "malformed"}
        self.faults = dict(faults or {})
        self.ledger: list[tuple[str, dict]] = []  # bu oturumda araçların ürettiği çıktılar (imza için)
        self._local = threading.local()

    # ---------- Yardımcılar ----------

    def _maybe_fail(self, name: str) -> None:
        if getattr(self._local, "internal", False):
            return  # aracın kendi iç hesabı; paralel agent'lar için thread'e özgü
        mode = self.faults.get(name)
        if mode == "timeout_once":
            self.faults.pop(name)
            raise TransientToolError(f"{name}: simüle edilmiş zaman aşımı")
        if mode == "slow_once":
            self.faults.pop(name)
            time.sleep(1.0)
        if mode == "error":
            raise ToolError(f"{name}: simüle edilmiş servis hatası")

    def _qf(self, window: dict, filters: dict | None = None, **extra) -> QueryFilter:
        start, end = validate_window(window["start"], window["end"])
        return QueryFilter.make(start, end, {**(filters or {}), **extra})

    def _stats(self, f: QueryFilter) -> dict:
        return with_rates(self.repo.summary(f))

    def _ctx(self, cw: dict, bw: dict | None, filters: dict) -> D.Ctx:
        return D.Ctx(window=_win(cw), baseline_window=_win(bw) if bw else None, scope=dict(filters))

    def _coverage_ratio(self, f: QueryFilter) -> float:
        bounds = self.repo.time_bounds()
        if bounds is None:
            return 0.0
        lo, hi = max(f.start, bounds[0]), min(f.end, bounds[1] + timedelta(seconds=1))
        return round(max(0.0, (hi - lo).total_seconds()) / (f.end - f.start).total_seconds(), 3)

    def _volume(self, cf: QueryFilter, bf: QueryFilter, cur: dict, base: dict) -> dict:
        """Pencere uzunlukları farklı olabileceği için hacim dakika başına karşılaştırılır (veriyle kapsanan süre)."""
        cur_min = cf.minutes * self._coverage_ratio(cf)
        base_min = bf.minutes * self._coverage_ratio(bf)
        cur_rate = round(cur["total"] / cur_min, 2) if cur_min else None
        base_rate = round(base["total"] / base_min, 2) if base_min else None
        return {"current_per_min": cur_rate, "baseline_per_min": base_rate, "change_pct": pct_change(cur_rate, base_rate)}

    # ---------- Araçlar ----------

    def check_data_coverage(self, window: dict, filters: dict) -> dict:
        self._maybe_fail("check_data_coverage")
        f = self._qf(window, filters)
        stats = self.repo.summary(f)
        quality = self.repo.field_quality(f)
        d1 = self.repo.summary(f.shifted(timedelta(days=-1)))
        # Kapsam: veri sınırları içinde kalan pencere oranı × olay içeren 5 dk dilimlerin oranı (iç boşlukları da yakalar).
        # Çok düşük hacimde boş dilimler doğaldır; bu yüzden dilim kontrolü yalnızca beklenen hacim yeterliyse uygulanır.
        coverage = self._coverage_ratio(f)
        buckets = self.repo.histogram(f, BUCKET_MINUTES)
        empty = sum(1 for b in buckets if b["events"] == 0)
        expected_per_bucket = stats["events"] / len(buckets) if buckets else 0
        if buckets and expected_per_bucket >= 10:
            coverage = round(coverage * (len(buckets) - empty) / len(buckets), 3)
        null_rates = {k: v for k, v in quality["null_rates"].items() if k in QUALITY_FIELDS}
        max_null = max((v for v in null_rates.values() if v is not None), default=0.0)
        level, issues = assess_evidence(stats["total"], stats["total"], self.th, coverage, max_null)
        if not d1["total"]:
            issues.append("Dünün aynı penceresi için veri yok; hacimde gün içi sezonsallık hesaba katılamaz.")
        return {
            "window": _win(window), "filters": f.as_dict(), "events": stats["events"],
            "primary_transactions": stats["total"], "coverage_ratio": coverage, "empty_buckets": empty,
            "d1_available": d1["total"] > 0, "d1_primary_transactions": d1["total"], "null_rates": null_rates,
            "evidence_quality": level, "issues": issues,
            "data_label": "synthetic" if self.repo.name == "synthetic" else "external",
        }

    def get_payment_metrics(self, window: dict, filters: dict) -> dict:
        self._maybe_fail("get_payment_metrics")
        f = self._qf(window, filters)
        return {"window": _win(window), "filters": f.as_dict(), **self._stats(f)}

    def compare_with_baseline(self, current_window: dict, baseline_window: dict, filters: dict) -> dict:
        self._maybe_fail("compare_with_baseline")
        cf, bf = self._qf(current_window, filters), self._qf(baseline_window, filters)
        cur, base = self._stats(cf), self._stats(bf)
        ctx = self._ctx(current_window, baseline_window, cf.as_dict())
        detections = [D.rate_spike(name, cur, base, ctx, self.th) for name in D.RATE_SPECS]
        detections.append(D.latency_increase(cur, base, ctx, self.th))
        # Hacim: gün içi sezonsallık için dünün aynı penceresi; yoksa önceki pencere
        cur_buckets = self.repo.histogram(cf, BUCKET_MINUTES)
        ref_f = cf.shifted(timedelta(days=-1))
        ref_buckets = self.repo.histogram(ref_f, BUCKET_MINUTES)
        method = "day_over_day"
        if sum(b["total"] for b in ref_buckets) == 0:
            ref_buckets, method = self.repo.histogram(bf, BUCKET_MINUTES), "trailing"
        detections.append(D.volume_anomaly(cur_buckets, ref_buckets, ctx, self.th, method))
        timeline = {det["detector"]: D.onset_duration(cur_buckets, base, det["detector"], self.th, BUCKET_MINUTES)
                    for det in detections if det["detected"] and det["detector"] in D.RATE_SPECS}
        return {
            "window": ctx.window, "baseline_window": ctx.baseline_window, "filters": cf.as_dict(),
            "current": cur, "baseline": base,
            "deltas": {
                "technical_pp": rate_delta_pp(cur["technical_rate"], base["technical_rate"]),
                "decline_pp": rate_delta_pp(cur["decline_rate"], base["decline_rate"]),
                "timeout_pp": rate_delta_pp(cur["timeout_rate"], base["timeout_rate"]),
                "p95_change_pct": pct_change(cur["p95_ms"], base["p95_ms"]),
                "reversal_ratio_change_pct": pct_change(cur["reversal_ratio"], base["reversal_ratio"]),
            },
            "excess": {k: excess_count(cur[k], cur["total"], base[k], base["total"]) for k in ("technical", "declined", "timeouts", "reversals")},
            "volume": self._volume(cf, bf, cur, base),
            "volume_reference": method,
            "timeline": timeline,
            "detections": detections,
        }

    def breakdown_by_dimension(self, current_window: dict, baseline_window: dict, dimension: str, filters: dict,
                               metric: str = "technical_rate", response_code: str | None = None) -> dict:
        self._maybe_fail("breakdown_by_dimension")
        return self._breakdown(current_window, baseline_window, dimension, filters, metric, response_code)

    def _breakdown(self, current_window: dict, baseline_window: dict, dimension: str, filters: dict,
                   metric: str, response_code: str | None) -> dict:
        if metric == "response_code" and not response_code:
            raise ValueError("metric=response_code için response_code gerekli.")
        cf, bf = self._qf(current_window, filters), self._qf(baseline_window, filters)
        cur_groups = self.repo.group_by(cf, dimension, 20)
        base_groups = self.repo.group_by(bf, dimension, 20)
        if metric == "response_code":
            for groups, f in ((cur_groups, cf), (base_groups, bf)):
                counts = {g["key"]: g["total"] for g in self.repo.group_by(f.with_filters(response_code=response_code), dimension, 20)}
                for g in groups:
                    g["code"] = counts.get(g["key"], 0)
        ctx = self._ctx(current_window, baseline_window, cf.as_dict())
        label = f"response_code:{response_code}" if metric == "response_code" else metric
        result = D.isolate(dimension, label, cur_groups, base_groups, ctx, self.th, COUNT_KEYS[metric])
        return {"window": ctx.window, "baseline_window": ctx.baseline_window, "filters": cf.as_dict(),
                "response_code": response_code, **result}

    def analyze_response_codes(self, current_window: dict, baseline_window: dict, filters: dict,
                               dimension: str | None = None) -> dict:
        self._maybe_fail("analyze_response_codes")
        cf, bf = self._qf(current_window, filters), self._qf(baseline_window, filters)
        cur, base = self.repo.summary(cf), self.repo.summary(bf)
        ctx = self._ctx(current_window, baseline_window, cf.as_dict())
        rows, detections = D.response_codes(self.repo.top_terms(cf, "response_code", 20),
                                            self.repo.top_terms(bf, "response_code", 20),
                                            cur["total"], base["total"], ctx, self.th)
        isolation = None
        if dimension and detections:
            top = max(detections, key=lambda d: d["statistic"] or 0)
            isolation = self._breakdown(current_window, baseline_window, dimension, filters,
                                        "response_code", top["subject"])
        return {"window": ctx.window, "baseline_window": ctx.baseline_window, "filters": cf.as_dict(),
                "codes": rows, "detections": detections, "dimension": dimension, "isolation": isolation}

    def analyze_latency_timeouts(self, current_window: dict, baseline_window: dict, filters: dict) -> dict:
        self._maybe_fail("analyze_latency_timeouts")
        cf, bf = self._qf(current_window, filters), self._qf(baseline_window, filters)
        cur, base = self._stats(cf), self._stats(bf)
        ctx = self._ctx(current_window, baseline_window, cf.as_dict())
        # Korelasyon için önceki eşit uzunlukta dönem de dahil edilir (baseline sınırını aşmadan)
        ext_start = max(bf.start, cf.start - (cf.end - cf.start))
        buckets = self.repo.histogram(QueryFilter(ext_start, cf.end, cf.filters), BUCKET_MINUTES)
        corr = D.latency_timeout_correlation(buckets, base, ctx, self.th, BUCKET_MINUTES)
        detections = [corr.pop("detection"), D.latency_increase(cur, base, ctx, self.th),
                      D.rate_spike("TIMEOUT_RATE_SPIKE", cur, base, ctx, self.th)]
        slim = [{"bucket_start": b["bucket_start"], "total": b["total"], "p95_ms": b["p95_ms"], "timeouts": b["timeouts"],
                 "errors": b["errors"]} for b in buckets]
        return {"window": ctx.window, "baseline_window": ctx.baseline_window, "filters": cf.as_dict(),
                "current_p95_ms": cur["p95_ms"], "baseline_p95_ms": base["p95_ms"],
                "current_timeout_rate": cur["timeout_rate"], "baseline_timeout_rate": base["timeout_rate"],
                **corr, "buckets": slim, "detections": detections}

    def analyze_reversals(self, current_window: dict, baseline_window: dict, filters: dict) -> dict:
        self._maybe_fail("analyze_reversals")
        cf, bf = self._qf(current_window, filters), self._qf(baseline_window, filters)
        cur_link = self.repo.reversal_linkage(cf, REVERSAL_LOOKBACK_MIN, self.th.auto_reversal_max_s)
        base_link = self.repo.reversal_linkage(bf, REVERSAL_LOOKBACK_MIN, self.th.auto_reversal_max_s)
        cur, base = self.repo.summary(cf), self.repo.summary(bf)
        ctx = self._ctx(current_window, baseline_window, cf.as_dict())
        det = D.reversal_anomaly(cur_link, base_link, cur["total"], base["total"], ctx, self.th)
        return {"window": ctx.window, "baseline_window": ctx.baseline_window, "filters": cf.as_dict(),
                "current": cur_link, "baseline": base_link, "primary_current": cur["total"],
                "primary_baseline": base["total"], "detection": det,
                "reconciliation_risk": D.reconciliation_risk(cur_link),
                "top_merchants": cur_link["technical_by_merchant"]}

    def compare_service_versions(self, window: dict, service_name: str, filters: dict) -> dict:
        self._maybe_fail("compare_service_versions")
        filters = {k: v for k, v in filters.items() if k not in ("service_name", "service_version")}
        f = self._qf(window, filters, service_name=service_name)
        versions = self.repo.group_by(f, "service_version", 20)
        lookback = QueryFilter(f.end - timedelta(hours=6), f.end, f.filters)
        data_start = (self.repo.time_bounds() or (lookback.start,))[0]
        deployments = []
        for v in versions:
            seen = self.repo.bounds(lookback.with_filters(service_version=v["key"]))
            first = seen[0] if seen else None
            new = bool(first and first > max(lookback.start, data_start) + timedelta(minutes=5))
            entry = {"version": v["key"], "first_seen": to_iso(first) if first else None, "new_in_lookback": new}
            if new:
                before = self._stats(QueryFilter(first - timedelta(minutes=60), first, f.filters))
                after = self._stats(QueryFilter(first, f.end, f.filters))
                entry["service_before"] = {"total": before["total"], "technical_rate": before["technical_rate"]}
                entry["service_after"] = {"total": after["total"], "technical_rate": after["technical_rate"]}
            deployments.append(entry)
        ctx = self._ctx(window, None, f.as_dict())
        result = D.version_comparison(service_name, versions, deployments, ctx, self.th)
        return {"window": ctx.window, "filters": f.as_dict(), **result}

    def search_transactions(self, window: dict, filters: dict, transaction_status: str | None = None,
                            response_code: str | None = None, limit: int = 10, cursor: str | None = None) -> dict:
        self._maybe_fail("search_transactions")
        f = self._qf(window, filters, transaction_status=transaction_status, response_code=response_code)
        page = self.repo.sample(f, limit, cursor)
        warnings = [] if page["total"] else ["Bu filtrelerle eşleşen işlem bulunamadı."]
        return {"window": _win(window), "filters": f.as_dict(), "total_matches": page["total"],
                "returned": len(page["records"]), "next_cursor": page["next_cursor"], "samples": page["records"],
                "warnings": warnings}

    def search_similar_incidents(self, current_window: dict, baseline_window: dict, filters: dict) -> dict:
        """İmza deterministik araç çıktılarından kurulur; LLM'in yazdığı metin veya sayılar kullanılmaz."""
        self._maybe_fail("search_similar_incidents")
        outputs = self._core_evidence(current_window, baseline_window, filters)
        window = outputs[0]["window"]
        outputs += [o for _, o in self.ledger if o.get("window") == window]
        return find_similar(build_signature(outputs), self.incidents)

    def _core_evidence(self, cw: dict, bw: dict, filters: dict) -> list[dict]:
        """İmza için temel kanıtı (baseline, segment, yanıt kodu, reversal, sürüm) deterministik olarak yeniden hesaplar.
        Hata enjeksiyonu uygulanmaz; bu, aracın kendi iç hesabıdır."""
        self._local.internal = True
        try:
            baseline = self.compare_with_baseline(cw, bw, filters)
            outputs = [baseline]
            names = {d["detector"] for d in baseline["detections"] if d["detected"]}
            if names & {"TECHNICAL_FAILURE_SPIKE", "TIMEOUT_RATE_SPIKE"}:
                for dim in ("bank_id", "service_name"):
                    if dim not in filters:
                        out = self._breakdown(cw, bw, dim, filters, "technical_rate", None)
                        outputs.append(out)
                        if out["isolated"]:
                            if dim == "service_name":
                                outputs.append(self.compare_service_versions(cw, out["isolated_segment"], filters))
                            break
            if names - {"VOLUME_ANOMALY", "LATENCY_INCREASE"}:
                outputs.append(self.analyze_response_codes(cw, bw, filters, "bank_id" if "bank_id" not in filters else None))
            reversal = self.analyze_reversals(cw, bw, filters)
            if reversal["detection"]["detected"]:
                outputs.append(reversal)
                if "merchant_id" not in filters:
                    outputs.append(self._breakdown(cw, bw, "merchant_id", filters, "reversal_ratio", None))
            return outputs
        finally:
            self._local.internal = False

    def search_runbooks(self, query: str) -> dict:
        self._maybe_fail("search_runbooks")
        return self.retriever.search(query)

    def submit_findings(self, findings: list[str]) -> dict:
        """Agent'ın bulgu özetini kaydeder. Bulgular rapordan önce araç çıktılarına karşı doğrulanır."""
        self._maybe_fail("submit_findings")
        return {"accepted_findings": [f.strip() for f in findings if isinstance(f, str) and f.strip()][:8]}

    # ---------- Kayıt ----------

    def registry(self) -> dict[str, tuple[Callable, type[BaseModel], str]]:
        return {
            "check_data_coverage": (self.check_data_coverage, WindowArgs,
                                    "Pencerenin veri kapsamını, işlem sayısını, D-1 verisinin varlığını ve anahtar alanlardaki boş değer oranlarını kontrol eder."),
            "get_payment_metrics": (self.get_payment_metrics, WindowArgs,
                                    "Bir pencere için işlem hacmi, onay/red/teknik hata/timeout oranları ve p50/p95/p99 işlem süresini hesaplar."),
            "compare_with_baseline": (self.compare_with_baseline, BaselineArgs,
                                      "Güncel dönemi baseline ile karşılaştırır: teknik hata, red, timeout, gecikme ve hacim (D-1) tespitleri."),
            "breakdown_by_dimension": (self.breakdown_by_dimension, BreakdownArgs,
                                       "Bir metriği banka/üye işyeri/servis/sürüm gibi bir boyutta segmentlere ayırır ve sorunun tek segmente izole olup olmadığını test eder."),
            "analyze_response_codes": (self.analyze_response_codes, ResponseCodeArgs,
                                       "Yanıt kodlarının baseline'a göre olağandışı artışını bulur; istenirse en olağandışı kodu bir boyutta kırar."),
            "analyze_latency_timeouts": (self.analyze_latency_timeouts, BaselineArgs,
                                         "Gecikme ile timeout/hata oranının aynı dönem ve kapsamda artıp artmadığını, hangisinin önce başladığını ölçer (korelasyon)."),
            "analyze_reversals": (self.analyze_reversals, BaselineArgs,
                                  "Reversal'ları orijinal işleme bağlayıp sınıflandırır; teknik reversal anomalisi ve mutabakat riskini değerlendirir."),
            "compare_service_versions": (self.compare_service_versions, VersionArgs,
                                         "Bir servisin aynı penceredeki sürümlerini karşılaştırır ve deployment zamanını yaklaşık belirler (hipotez)."),
            "search_transactions": (self.search_transactions, TransactionSearchArgs,
                                    "Filtrelere uyan işlemlerden sayfalı, en fazla 50 adet, maskelenmiş örnek kayıt getirir."),
            "search_similar_incidents": (self.search_similar_incidents, BaselineArgs,
                                         "Güncel anomali imzasını geçmiş incident kayıtlarıyla karşılaştırır ve benzerlik gerekçesini döner."),
            "search_runbooks": (self.search_runbooks, RunbookArgs, "Operasyon runbook'larında kaynaklı (doküman › bölüm) arama yapar."),
            "submit_findings": (self.submit_findings, FindingsArgs,
                                "Analiz bittiğinde araç sonuçlarına dayanan bulguları rapora gönderir. En son çağrılır."),
        }

    def call(self, name: str, args: dict) -> dict:
        """İzin verilen araçlardan birini doğrulanmış argümanlarla çalıştırır."""
        registry = self.registry()
        if name not in registry:
            raise ToolError(f"İzin verilmeyen araç: {name}")
        func, schema, _ = registry[name]
        parsed = schema.model_validate(args)
        kwargs = parsed.model_dump()
        if "filters" in kwargs:
            kwargs["filters"] = parsed.filters.active()
        logger.info("tool=%s args=%s", name, json.dumps(kwargs, ensure_ascii=False))
        output = func(**kwargs)
        if self.faults.get(name) == "malformed":
            output = {"unexpected": True}
        self.ledger.append((name, output))
        return output

    def as_langchain_tools(self) -> list:
        """LLM'e bind_tools ile verilecek şemalar. Çalıştırma agent içindeki executor'da yapılır."""
        from langchain_core.tools import StructuredTool
        return [
            StructuredTool.from_function(func=func, name=name, description=desc, args_schema=schema)
            for name, (func, schema, desc) in self.registry().items()
        ]


# ---------- Tespit toplama ve incident imzası ----------


def detections_of(tool: str, output: dict) -> list[dict]:
    """Bir araç çıktısındaki tüm tespit sözlüklerini döner."""
    if tool in ("compare_with_baseline", "analyze_response_codes", "analyze_latency_timeouts"):
        found = list(output.get("detections", []))
        if tool == "analyze_response_codes" and output.get("isolation"):
            found.append(output["isolation"]["detection"])
        return found
    if tool in ("breakdown_by_dimension", "analyze_reversals", "compare_service_versions"):
        return [output["detection"]]
    return []


def _all_detections(outputs: list[dict]) -> list[dict]:
    found = []
    for o in outputs:
        found += o.get("detections", [])
        if isinstance(o.get("detection"), dict):
            found.append(o["detection"])
        if isinstance(o.get("isolation"), dict):
            found.append(o["isolation"]["detection"])
    return found


def build_signature(outputs: list[dict]) -> dict:
    """Deterministik araç çıktılarından karşılaştırılabilir bir anomali imzası çıkarır."""
    detected = [d for d in _all_detections(outputs) if d["detected"]]
    baseline = next((o for o in outputs if "deltas" in o), None)
    scope: dict[str, str] = {}
    for d in detected:
        if d["detector"] in ("SEGMENT_ISOLATION", "SERVICE_VERSION_DEGRADATION"):
            for key in ("bank_id", "merchant_id", "service_name", "service_version"):
                if key in d["scope"] and key not in (baseline or {}).get("filters", {}):
                    scope.setdefault(key, d["scope"][key])
    reversal = next((d for d in detected if d["detector"] == "REVERSAL_ANOMALY"), None)
    deltas = (baseline or {}).get("deltas", {})
    names = sorted({d["detector"] for d in detected})
    codes = sorted({d["subject"] for d in detected if d["detector"] == "RESPONSE_CODE_CONCENTRATION" and d["subject"]})
    return {
        "detectors": names,
        "scope": scope,
        "response_codes": codes,
        "metrics": {"technical_delta_pp": deltas.get("technical_pp"), "decline_delta_pp": deltas.get("decline_pp"),
                    "p95_change_pct": deltas.get("p95_change_pct"),
                    "technical_reversals": reversal["extra"]["technical"] if reversal else 0},
        "description": " ".join(d["evidence"] for d in detected)[:600],
    }


def window_minutes(window: dict) -> int:
    return int((parse_timestamp(window["end"]) - parse_timestamp(window["start"])).total_seconds() // 60)
