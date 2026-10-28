"""
Araç girdi/çıktıları ve rapor için Pydantic şemaları.
"""

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ---------- Araç argümanları (LLM'e tool şeması olarak verilir) ----------


class TimeWindow(BaseModel):
    start: str = Field(description="ISO-8601 başlangıç, ör. 2026-10-27T11:30:00Z")
    end: str = Field(description="ISO-8601 bitiş, ör. 2026-10-27T12:00:00Z")


def _coerce_window(value: object) -> object:
    """Küçük modeller pencereyi 'başlangıç/bitiş' metni olarak da gönderebilir."""
    if isinstance(value, str) and "/" in value:
        start, end = value.split("/", 1)
        return {"start": start.strip(), "end": end.strip()}
    return value


class _WindowPairArgs(BaseModel):
    current_window: TimeWindow = Field(description="İncelenen dönem")
    baseline_window: TimeWindow = Field(description="Karşılaştırma (normal) dönemi")
    service: str | None = Field(default=None, description="Servis adı filtresi (opsiyonel)")

    @field_validator("current_window", "baseline_window", mode="before")
    @classmethod
    def _parse_window(cls, value: object) -> object:
        return _coerce_window(value)


class SearchLogsArgs(BaseModel):
    start_time: str = Field(description="ISO-8601 başlangıç zamanı")
    end_time: str = Field(description="ISO-8601 bitiş zamanı")
    service: str | None = Field(default=None, description="Servis adı filtresi (opsiyonel)")
    error_code: str | None = Field(default=None, description="Hata kodu filtresi (opsiyonel)")
    status: Literal["success", "failed"] | None = Field(default=None, description="İşlem durumu filtresi (opsiyonel)")
    limit: int = Field(default=10, ge=1, le=50, description="Dönecek örnek kayıt sayısı (en fazla 50)")
    cursor: int | None = Field(default=None, description="Sonraki sayfa için önceki yanıttaki next_cursor")


class MetricsArgs(BaseModel):
    start_time: str = Field(description="ISO-8601 başlangıç zamanı")
    end_time: str = Field(description="ISO-8601 bitiş zamanı")
    service: str | None = Field(default=None, description="Servis adı filtresi (opsiyonel)")


class CompareArgs(_WindowPairArgs):
    pass


class PatternArgs(_WindowPairArgs):
    pass


class SimilarIncidentArgs(_WindowPairArgs):
    pass


class RunbookArgs(BaseModel):
    query: str = Field(description="Runbook araması, ör. 'PROVIDER_TIMEOUT provider-connector'")


class ReportArgs(BaseModel):
    findings: list[str] = Field(description="Araç sonuçlarına dayanan kısa bulgu cümleleri")

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


class MetricsResult(BaseModel):
    window_start: str
    window_end: str
    service: str | None = None
    total_attempts: int
    successful: int
    failed: int
    error_rate: float | None
    avg_latency_ms: float | None
    p95_latency_ms: float | None
    sufficient_data: bool


class ComparisonResult(BaseModel):
    current: MetricsResult
    baseline: MetricsResult
    error_rate_delta_pp: float | None
    error_rate_ratio: float | None
    p95_change_pct: float | None
    avg_latency_change_pct: float | None
    volume_per_min_current: float | None
    volume_per_min_baseline: float | None
    volume_change_pct: float | None
    excess_failed: int


class CodeItem(BaseModel):
    key: str
    count: int
    share: float
    baseline_count: int


class Pattern(BaseModel):
    type: str
    detected: bool
    subject: str | None
    metric: str
    current: float | int | list | None
    baseline: float | int | list | None
    rule: str
    sample_size: int
    insufficient_data: bool
    statistic: float | None
    evidence: str


class PatternResult(BaseModel):
    window_start: str
    window_end: str
    baseline_start: str
    baseline_end: str
    service: str | None
    total_failed: int
    top_error_codes: list[CodeItem]
    patterns: list[Pattern]
    detected_types: list[str]
    affected_services: list[str]
    services: list[dict]
    buckets: list[dict]
    onset_time: str | None
    duration_min: int
    ongoing: bool
    onset_shape: str | None
    anomalous_buckets: list[str]
    threshold_note: str


class LogSearchResult(BaseModel):
    window_start: str
    window_end: str
    filters: dict[str, str | None]
    total_matches: int
    returned: int
    next_cursor: int | None
    samples: list[dict]
    warnings: list[str] = []


class SimilarIncidentResult(BaseModel):
    signature: dict
    matches: list[dict]
    found: bool
    message: str
    disclaimer: str


class RunbookHit(BaseModel):
    source: str
    section: str
    text: str
    score: float
    flagged: bool = False


class RunbookSearchResult(BaseModel):
    query: str
    found: bool
    hits: list[RunbookHit]
    procedures: list[dict] = []
    message: str


class ReportToolResult(BaseModel):
    accepted_findings: list[str]
    rejected_findings: list[str]


TOOL_OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "search_payment_logs": LogSearchResult,
    "calculate_payment_metrics": MetricsResult,
    "compare_time_windows": ComparisonResult,
    "analyze_error_patterns": PatternResult,
    "search_similar_incidents": SimilarIncidentResult,
    "search_runbooks": RunbookSearchResult,
    "generate_incident_report": ReportToolResult,
}

# ---------- Risk ve rapor ----------

RiskLevel = Literal["LOW", "MEDIUM", "HIGH", "INDETERMINATE"]
ConfidenceLevel = Literal["LOW", "MEDIUM", "HIGH"]


class RiskReason(BaseModel):
    factor: str
    value: str
    points: int
    explanation: str


class RiskAssessment(BaseModel):
    level: RiskLevel
    score: int | None
    reasons: list[RiskReason]
    confidence: ConfidenceLevel
    confidence_reasons: list[str]
    note: str


class Observation(BaseModel):
    id: str
    text: str
    evidence: str


class Explanation(BaseModel):
    text: str
    evidence_refs: list[str]
    confidence: Literal["low", "medium"]


class Recommendation(BaseModel):
    text: str
    source: str
    evidence_refs: list[str] = []


class Deviation(BaseModel):
    metric: str
    current: str
    baseline: str
    change: str


class IncidentReport(BaseModel):
    title: str
    generated_at: str
    mode: str
    data_source: str
    scope: dict
    data_coverage: dict
    assumptions: list[str]
    risk: RiskAssessment
    current: MetricsResult | None
    baseline: MetricsResult | None
    comparison: ComparisonResult | None
    detected_patterns: list[Pattern]
    evaluated_patterns: list[Pattern]
    deviations: list[Deviation]
    affected_services: list[dict]
    affected_error_codes: list[CodeItem]
    similar_incidents: list[dict]
    similar_incidents_note: str
    observations: list[Observation]
    possible_explanations: list[Explanation]
    confirmed_root_cause: str | None
    recommended_steps: list[Recommendation]
    agent_findings: list[str]
    runbook_sources: list[str]
    limitations: list[str]
    validation: list[dict]
    disclaimer: str
