"""
Çoklu agent iletişim sözleşmesi: tüm agent girdi ve çıktıları Pydantic ile doğrulanır.

Gerçeğin kaynağı agent mesajları değil, EvidenceStore'daki araç sonuçlarıdır. Her bulgu en az bir kanıt
referansına (ör. "E3") bağlanmalıdır; referansı olmayan veya var olmayan bir referansa dayanan bulgu
sentezde reddedilir.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.domain import DIMENSIONS

AgentName = Literal["anomaly_detection", "payment_flow", "historical_incident", "impact_risk"]
AGENT_NAMES: tuple[str, ...] = ("anomaly_detection", "payment_flow", "historical_incident", "impact_risk")
AgentStatus = Literal["success", "partial", "insufficient_data", "failed"]


class ExecutionBudget(BaseModel):
    """Yapılandırılabilir yürütme sınırları."""

    max_rounds: int = Field(default=2, ge=1, le=5, description="İlk tur + hedefli takip turları")
    max_tool_calls: int = Field(default=30, ge=1, le=200)
    tool_timeout_s: float = Field(default=10.0, gt=0)
    agent_timeout_s: float = Field(default=30.0, gt=0)
    retries: int = Field(default=1, ge=0, le=3, description="Geçici araç hatasında ek deneme")
    max_parallel: int = Field(default=3, ge=1, le=8)


class TimeRange(BaseModel):
    current_start: str
    current_end: str
    window_minutes: int


class BaselineDefinition(BaseModel):
    start: str
    end: str
    minutes: int
    mode: Literal["previous", "d1"]
    volume_reference: str = "Hacim için dünün aynı penceresi (yoksa önceki pencere)"


class InvestigationContext(BaseModel):
    """Tüm agent'ların paylaştığı inceleme bağlamı."""

    investigation_id: str
    user_question: str
    time_range: TimeRange
    filters: dict[str, str] = {}
    data_source: str
    baseline_definition: BaselineDefinition
    shared_evidence_references: list[str] = []
    execution_budget: ExecutionBudget = ExecutionBudget()

    def windows(self) -> tuple[dict, dict]:
        return ({"start": self.time_range.current_start, "end": self.time_range.current_end},
                {"start": self.baseline_definition.start, "end": self.baseline_definition.end})


class AgentTask(BaseModel):
    """Orkestratörün bir uzmana verdiği görev."""

    agent: AgentName
    round: int = 1
    objective: str
    focus_filters: dict[str, str] = {}
    questions: list[str] = []
    reason: str = ""

    @field_validator("focus_filters")
    @classmethod
    def _known_fields(cls, value: dict[str, str]) -> dict[str, str]:
        allowed = set(DIMENSIONS) | {"transaction_type"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"İzin verilmeyen odak alanı: {', '.join(sorted(unknown))}")
        return value


class Finding(BaseModel):
    """Ölçülmüş bir gözlem veya korelasyon. Kanıt referansı zorunludur."""

    claim: str
    claim_type: Literal["observation", "correlation"] = "observation"
    detector: str | None = None
    scope: dict[str, str] = {}
    evidence_refs: list[str] = Field(min_length=1)


class AgentHypothesis(BaseModel):
    text: str
    evidence_refs: list[str] = Field(min_length=1)


class Assertion(BaseModel):
    """Çelişki tespiti için makinece karşılaştırılabilir iddia: bir metrik bir boyutta tek segmente izole mi?"""

    dimension: str
    metric: str
    isolated_segment: str | None
    evidence_refs: list[str] = Field(min_length=1)


class FollowUp(BaseModel):
    agent: AgentName
    reason: str
    focus_filters: dict[str, str] = {}
    questions: list[str] = []


class AgentResult(BaseModel):
    """Her uzmanın döndürdüğü yapılandırılmış sonuç."""

    model_config = ConfigDict(extra="forbid")

    agent_name: AgentName
    status: AgentStatus
    summary: str
    findings: list[Finding] = []
    supporting_metrics: dict[str, float | int | str | None] = {}
    evidence_references: list[str] = []
    hypotheses: list[AgentHypothesis] = []
    assertions: list[Assertion] = []
    limitations: list[str] = []
    recommended_follow_up: list[FollowUp] = []
    errors: list[str] = []
    round: int = 1
    tool_calls: int = 0
    elapsed_ms: float = 0.0


class AgentSelection(BaseModel):
    """LLM planlayıcının (opsiyonel) yapılandırılmış çıktısı."""

    agents: list[AgentName]
    rationale: str = ""


class Conflict(BaseModel):
    kind: Literal["isolation", "detection", "evidence_quality", "incident_scope"]
    description: str
    agents: list[str]
    evidence_refs: list[str] = []
    follow_up: FollowUp | None = None
    resolved: bool = False
