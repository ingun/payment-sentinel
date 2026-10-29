"""
Araç girdi/çıktıları, risk ve rapor için Pydantic şemaları.
Girdi şemaları LLM'e tool şeması olarak verilir; çıktı şemaları executor'da doğrulanır.
"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.domain import CHANNELS, DIMENSIONS, PAYMENT_METHODS, SERVICES, TRANSACTION_STATUSES, TRANSACTION_TYPES

ID_PATTERN = r"^[A-Za-z0-9_.\-]{1,64}$"

# ---------- Araç argümanları ----------


class TimeWindow(BaseModel):
    start: str = Field(description="ISO-8601 başlangıç (UTC), ör. 2026-10-27T11:00:00Z")
    end: str = Field(description="ISO-8601 bitiş (UTC), ör. 2026-10-27T12:00:00Z")


def _coerce_window(value: object) -> object:
    """Küçük modeller pencereyi 'başlangıç/bitiş' metni olarak da gönderebilir."""
    if isinstance(value, str) and "/" in value:
        start, end = value.split("/", 1)
        return {"start": start.strip(), "end": end.strip()}
    return value


class ScopeFilters(BaseModel):
    """Kapsam filtreleri; yalnızca izinli alanlar ve güvenli değerler kabul edilir."""

    model_config = ConfigDict(extra="forbid")

    bank_id: str | None = Field(default=None, pattern=ID_PATTERN, description="Banka, ör. BANK_B")
    merchant_id: str | None = Field(default=None, pattern=ID_PATTERN, description="Üye işyeri, ör. M-007")
    channel: Literal[tuple(CHANNELS)] | None = Field(default=None, description="POS veya ECOM")  # type: ignore[valid-type]
    payment_method: Literal[tuple(PAYMENT_METHODS)] | None = None  # type: ignore[valid-type]
    service_name: Literal[tuple(SERVICES)] | None = None  # type: ignore[valid-type]
    service_version: str | None = Field(default=None, pattern=ID_PATTERN)
    transaction_type: Literal[tuple(TRANSACTION_TYPES)] | None = None  # type: ignore[valid-type]

    def active(self) -> dict[str, str]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


def _coerce_filters(value: object) -> object:
    if value in (None, "", "null", "{}"):
        return {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if v not in (None, "", "all", "tümü")}
    return value


class _FilterArgs(BaseModel):
    filters: ScopeFilters = Field(default_factory=ScopeFilters, description="Kapsam filtreleri (opsiyonel)")

    @field_validator("filters", mode="before")
    @classmethod
    def _parse_filters(cls, value: object) -> object:
        return _coerce_filters(value)


class WindowArgs(_FilterArgs):
    window: TimeWindow = Field(description="İncelenen dönem")

    @field_validator("window", mode="before")
    @classmethod
    def _parse_window(cls, value: object) -> object:
        return _coerce_window(value)


class BaselineArgs(_FilterArgs):
    current_window: TimeWindow = Field(description="İncelenen dönem")
    baseline_window: TimeWindow = Field(description="Karşılaştırma (normal) dönemi")

    @field_validator("current_window", "baseline_window", mode="before")
    @classmethod
    def _parse_window(cls, value: object) -> object:
        return _coerce_window(value)


BreakdownMetric = Literal["technical_rate", "decline_rate", "timeout_rate", "reversal_ratio", "response_code"]


class BreakdownArgs(BaselineArgs):
    dimension: Literal[tuple(DIMENSIONS)] = Field(description="Kırılım boyutu, ör. bank_id")  # type: ignore[valid-type]
    metric: BreakdownMetric = Field(default="technical_rate", description="Karşılaştırılan metrik")
    response_code: str | None = Field(default=None, pattern=r"^[0-9A-Z]{2,4}$", description="metric=response_code ise kod")


class ResponseCodeArgs(BaselineArgs):
    dimension: Literal[tuple(DIMENSIONS)] | None = Field(  # type: ignore[valid-type]
        default=None, description="Olağandışı kodun segment kırılımı için boyut (opsiyonel)")


class VersionArgs(WindowArgs):
    service_name: Literal[tuple(SERVICES)] = Field(description="Sürümleri karşılaştırılacak servis")  # type: ignore[valid-type]


class TransactionSearchArgs(WindowArgs):
    transaction_status: Literal[tuple(TRANSACTION_STATUSES)] | None = None  # type: ignore[valid-type]
    response_code: str | None = Field(default=None, pattern=r"^[0-9A-Z]{2,4}$")
    limit: int = Field(default=10, ge=1, le=50, description="Dönecek örnek kayıt sayısı (en fazla 50)")
    cursor: str | None = Field(default=None, max_length=200, description="Sonraki sayfa için önceki yanıttaki next_cursor")


class RunbookArgs(BaseModel):
    query: str = Field(max_length=300, description="Runbook araması, ör. 'BANK_B timeout 68 POS'")


class FindingsArgs(BaseModel):
    findings: list[str] = Field(description="Araç sonuçlarına dayanan 3-6 kısa Türkçe bulgu")

    @field_validator("findings", mode="before")
    @classmethod
    def _parse_findings(cls, value: object) -> object:
        """Küçük modeller listeyi JSON metni olarak gönderebilir."""
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [str(v) for v in parsed]
            except json.JSONDecodeError:
                pass
            return [line.strip(" -•") for line in value.splitlines() if line.strip()]
        return value


# ---------- Araç çıktıları ----------


class _Out(BaseModel):
    model_config = ConfigDict(extra="allow")


class Detection(_Out):
    detector: str
    detected: bool
    subject: str | None
    metric: str
    observed: float | int | None
    baseline: float | int | None
    abs_deviation: float | None
    rel_deviation_pct: float | None
    window: dict
    baseline_window: dict | None
    scope: dict
    sample_size: dict
    evidence_quality: Literal["sufficient", "limited", "insufficient"]
    insufficient_data: bool
    statistic: float | None
    method: str
    limitations: list[str]
    claim_type: Literal["observation", "correlation", "hypothesis"]
    evidence: str


class Stats(_Out):
    total: int
    approved: int
    declined: int
    technical: int
    timeouts: int
    reversals: int
    p95_ms: float | None
    technical_rate: float | None
    decline_rate: float | None


class CoverageResult(_Out):
    window: dict
    filters: dict
    events: int
    primary_transactions: int
    coverage_ratio: float
    d1_available: bool
    null_rates: dict
    evidence_quality: Literal["sufficient", "limited", "insufficient"]
    issues: list[str]


class MetricsResult(Stats):
    window: dict
    filters: dict


class BaselineResult(_Out):
    window: dict
    baseline_window: dict
    filters: dict
    current: Stats
    baseline: Stats
    deltas: dict
    excess: dict
    detections: list[Detection]


class BreakdownResult(_Out):
    dimension: str
    metric: str
    segments: list[dict]
    isolated: bool
    isolated_segment: str | None
    verdict: str
    detection: Detection


class ResponseCodeResult(_Out):
    codes: list[dict]
    detections: list[Detection]
    isolation: dict | None


class LatencyResult(_Out):
    rho: float | None
    lead_minutes: int | None
    buckets: list[dict]
    detections: list[Detection]


class ReversalResult(_Out):
    current: dict
    baseline: dict
    detection: Detection
    reconciliation_risk: dict


class VersionResult(_Out):
    service: str
    versions: list[dict]
    deployments: list[dict]
    detection: Detection


class TransactionSearchResult(_Out):
    total_matches: int
    returned: int
    next_cursor: str | None
    samples: list[dict]
    warnings: list[str]


class SimilarIncidentResult(_Out):
    signature: dict
    matches: list[dict]
    found: bool
    message: str
    disclaimer: str


class RunbookHit(_Out):
    source: str
    section: str
    text: str
    score: float
    flagged: bool = False


class RunbookSearchResult(_Out):
    query: str
    found: bool
    hits: list[RunbookHit]
    procedures: list[dict] = []
    message: str


class FindingsResult(_Out):
    accepted_findings: list[str]


TOOL_OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "check_data_coverage": CoverageResult,
    "get_payment_metrics": MetricsResult,
    "compare_with_baseline": BaselineResult,
    "breakdown_by_dimension": BreakdownResult,
    "analyze_response_codes": ResponseCodeResult,
    "analyze_latency_timeouts": LatencyResult,
    "analyze_reversals": ReversalResult,
    "compare_service_versions": VersionResult,
    "search_transactions": TransactionSearchResult,
    "search_similar_incidents": SimilarIncidentResult,
    "search_runbooks": RunbookSearchResult,
    "submit_findings": FindingsResult,
}

# ---------- Risk ve rapor ----------

Severity = Literal["LOW", "MEDIUM", "HIGH", "INDETERMINATE"]
ConfidenceLevel = Literal["LOW", "MEDIUM", "HIGH"]


class RiskFactor(BaseModel):
    factor: str
    value: str
    points: int
    explanation: str


class RiskAssessment(BaseModel):
    severity: Severity
    score: int | None
    factors: list[RiskFactor]
    affected_scope: dict
    affected_transactions: int
    monetary_impact: str
    evidence_quality: str
    evidence_limitations: list[str]
    confidence: ConfidenceLevel
    confidence_reasons: list[str]
    capped: str | None = None
    formula: str
    note: str


class Fact(BaseModel):
    id: str
    text: str
    source: str  # "adım N: araç" biçiminde


class Hypothesis(BaseModel):
    text: str
    evidence_refs: list[str]
    status: Literal["doğrulanmadı"] = "doğrulanmadı"


class Recommendation(BaseModel):
    text: str
    source: str
    evidence_refs: list[str] = []


class InvestigationReport(BaseModel):
    title: str
    generated_at: str
    question: str
    status: Literal["complete", "incomplete", "insufficient_evidence"]
    mode: str
    data_source: str
    data_label: str
    scope: dict
    data_coverage: dict
    assumptions: list[str]
    executive_summary: list[str]
    key_metrics: list[dict]
    detections: list[Detection]
    evaluated: list[Detection]
    affected_scope: list[dict]
    response_codes: list[dict]
    latency_findings: list[str]
    reversal_findings: list[str]
    version_findings: list[str]
    similar_incidents: list[dict]
    similar_incidents_note: str
    runbook_sources: list[str]
    risk: RiskAssessment
    recommendations: list[Recommendation]
    facts: list[Fact]
    hypotheses: list[Hypothesis]
    confirmed_causes: list[str]
    agent_findings: list[str]
    limitations: list[str]
    tool_steps: list[dict]
    validation: list[dict]
    disclaimer: str
    # Yalnızca çoklu agent modunda doldurulur
    agent_runs: list[dict] = []
    conflicts: list[dict] = []
    orchestration: dict = {}
