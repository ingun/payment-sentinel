"""Çoklu agent mimarisi testleri. Gerçek LLM çağrısı yapılmaz; sahte agent'lar ve sahte LLM kullanılır."""

import asyncio

import pytest

from src.data_generator import SCENARIO_LABELS
from src.multiagent import ExecutionBudget, Orchestrator
from src.multiagent.agents import AnomalyDetectionAgent, PaymentFlowAgent, SpecialistAgent, default_agents
from src.multiagent.contracts import AgentHypothesis, AgentResult, Assertion, Finding
from src.schemas import InvestigationReport


def run(make_toolkit, scenario: str, request: str | None = None, budget: ExecutionBudget | None = None,
        agents: dict | None = None, llm=None, faults=None) -> dict:
    orch = Orchestrator(make_toolkit(scenario, faults=faults), llm=llm, budget=budget, agents=agents)
    return orch.run(SCENARIO_LABELS[scenario]["request"] if request is None else request)


def events(out: dict, kind: str) -> list[dict]:
    return [e for e in out["trace"] if e["kind"] == kind]


def agents_run(out: dict) -> list[tuple[str, int]]:
    return [(a["agent_name"], a["round"]) for a in out["agent_results"]]


def with_agent(agent: SpecialistAgent) -> dict:
    agents = default_agents()
    agents[agent.name] = agent
    return agents


# ---------- Görev dağıtımı ----------


def test_delegation_depends_on_question_and_findings(make_toolkit):
    normal = run(make_toolkit, "normal_baseline")
    assert agents_run(normal) == [("anomaly_detection", 1), ("impact_risk", 1)]
    assert {e["agent"] for e in events(normal, "skip")} == {"payment_flow", "historical_incident"}

    decline = run(make_toolkit, "response_code_spike")
    assert [a for a, _ in agents_run(decline)] == ["anomaly_detection", "historical_incident", "impact_risk"]

    reversal = run(make_toolkit, "reversal_anomaly")
    first_wave = events(reversal, "plan")[0]["agents"]
    assert first_wave == ["anomaly_detection", "payment_flow"]  # ödeme akışı sorusu: ödeme akışı agent'ı öncelikli

    timeout = run(make_toolkit, "bank_timeout")
    assert ("payment_flow", 1) in agents_run(timeout)  # ara bulgu (timeout) üzerine dağıtıldı
    pf = next(a for a in timeout["agent_results"] if a["agent_name"] == "payment_flow")
    assert any(r["args"]["filters"].get("bank_id") == "BANK_B" for r in timeout["results"]
               if r.get("agent") == "payment_flow" and r["tool"] == "analyze_reversals")
    assert pf["status"] == "success"


def test_insufficient_data_skips_specialists(make_toolkit):
    out = run(make_toolkit, "low_volume")
    rep = out["report"]
    assert rep["status"] == "insufficient_evidence" and rep["risk"]["severity"] == "INDETERMINATE"
    assert {e["agent"] for e in events(out, "skip")} == {"payment_flow", "historical_incident"}
    assert next(a for a in out["agent_results"] if a["agent_name"] == "anomaly_detection")["status"] == "insufficient_data"


# ---------- Paralel ve ardışık yürütme ----------


def test_independent_agents_run_in_parallel(make_toolkit):
    out = run(make_toolkit, "bank_timeout")
    parallel = events(out, "parallel")
    assert parallel and "payment_flow" in parallel[0]["detail"] and "historical_incident" in parallel[0]["detail"]
    starts = {e["node"]: e["t_ms"] for e in events(out, "agent_start") if e["round"] == 1}
    ends = {e["node"]: e["t_ms"] for e in events(out, "agent_end") if e["round"] == 1}
    # İkisi de diğeri bitmeden başladı
    assert starts["historical_incident"] < ends["payment_flow"] and starts["payment_flow"] < ends["historical_incident"]


class SlowAgent(SpecialistAgent):
    def __init__(self, name: str, delay: float):
        self.name, self.delay = name, delay

    async def run(self, task, ctx, runner):
        await asyncio.sleep(self.delay)
        return AgentResult(agent_name=self.name, status="success", summary="yavaş agent", round=task.round)


def test_parallel_wave_time_is_less_than_sum(make_toolkit):
    agents = default_agents()
    agents["payment_flow"] = SlowAgent("payment_flow", 0.6)
    agents["historical_incident"] = SlowAgent("historical_incident", 0.6)
    out = run(make_toolkit, "bank_timeout", agents=agents)
    # Yalnızca paralel dalgayı ölç (anomali agent'ının süresi makine yüküne bağlıdır)
    starts = [e["t_ms"] for e in events(out, "agent_start") if e["node"] in ("payment_flow", "historical_incident")]
    ends = [e["t_ms"] for e in events(out, "agent_end") if e["node"] in ("payment_flow", "historical_incident")]
    wave_ms = max(ends) - min(starts)
    assert 600 <= wave_ms < 1200  # sıralı olsaydı ≥ 1200 ms


def test_follow_up_round_is_triggered_by_intermediate_findings(make_toolkit):
    out = run(make_toolkit, "reversal_anomaly")
    assert out["rounds"] == 2
    follow = events(out, "follow_up")
    assert follow and follow[0]["agent"] == "anomaly_detection" and "M-007" in follow[0]["detail"]
    round2 = [r for r in out["results"] if r.get("round") == 2 and r["tool"] == "compare_with_baseline"]
    assert round2 and round2[0]["args"]["filters"] == {"merchant_id": "M-007"}
    # Takip, ödeme akışı bulgusundan sonra yapıldı (ardışık bağımlılık)
    pf_end = next(e["t_ms"] for e in events(out, "agent_end") if e["node"] == "payment_flow")
    r2_start = next(e["t_ms"] for e in events(out, "agent_start") if e["round"] == 2)
    assert r2_start >= pf_end


# ---------- Çelişkiler ----------


class ContradictingAgent(PaymentFlowAgent):
    """Gerçek bir araç sonucu alır ama izolasyon için farklı bir segment iddia eder."""

    async def run(self, task, ctx, runner):
        cw, bw = ctx.windows()
        _, ref = await runner.call(self.name, frozenset({"compare_with_baseline"}), "compare_with_baseline",
                                   {"current_window": cw, "baseline_window": bw, "filters": {}}, "çelişki testi", task.round)
        return AgentResult(agent_name=self.name, status="success", summary="BANK_D izole iddiası", round=task.round,
                           assertions=[Assertion(dimension="bank_id", metric="technical_rate", isolated_segment="BANK_D", evidence_refs=[ref])],
                           evidence_references=[ref])


def test_conflicting_outputs_trigger_targeted_verification(make_toolkit):
    out = run(make_toolkit, "bank_timeout", agents=with_agent(ContradictingAgent()))
    conflicts = out["report"]["conflicts"]
    iso = [c for c in conflicts if c["kind"] == "isolation"]
    assert iso and iso[0]["resolved"] and "BANK_B" in iso[0]["description"]
    assert out["rounds"] == 2
    verify = [r for r in out["results"] if r.get("round") == 2 and r["tool"] == "breakdown_by_dimension"]
    assert verify and verify[0]["output"]["isolated_segment"] == "BANK_B"


# ---------- Hatalar, zaman aşımı, geçersiz çıktı ----------


def test_agent_timeout_does_not_invalidate_other_findings(make_toolkit):
    # Zaman aşımı tüm agent'lara uygulanır: gerçek agent'lara yük altında da yetecek pay bırakılır
    agents = with_agent(SlowAgent("historical_incident", 30.0))
    out = run(make_toolkit, "bank_timeout", agents=agents, budget=ExecutionBudget(agent_timeout_s=3.0, max_rounds=1))
    hist = next(a for a in out["agent_results"] if a["agent_name"] == "historical_incident")
    assert hist["status"] == "failed" and "zaman aşımı" in hist["errors"][0]
    rep = out["report"]
    assert rep["status"] == "incomplete"
    assert any("historical_incident agent'ı başarısız" in x for x in rep["limitations"])
    assert rep["risk"]["severity"] == "HIGH"  # anomali ve ödeme akışı bulguları korunur
    assert any(d["detector"] == "TIMEOUT_RATE_SPIKE" for d in rep["detections"])


def test_tool_failure_gives_partial_agent_and_disclosed_gap(make_toolkit):
    out = run(make_toolkit, "bank_timeout", faults={"search_runbooks": "error"})
    hist = next(a for a in out["agent_results"] if a["agent_name"] == "historical_incident")
    assert hist["status"] == "partial" and hist["errors"]
    assert out["report"]["status"] == "incomplete" and "search_runbooks" in " ".join(out["report"]["limitations"])


def test_alternative_tool_when_breakdown_fails(make_toolkit):
    out = run(make_toolkit, "bank_timeout", faults={"breakdown_by_dimension": "error"})
    anomaly = next(a for a in out["agent_results"] if a["agent_name"] == "anomaly_detection")
    assert anomaly["status"] == "partial"
    assert any(r["tool"] == "analyze_response_codes" and r["args"].get("dimension") == "bank_id" for r in out["results"])


class InvalidOutputAgent(SpecialistAgent):
    name = "historical_incident"

    async def run(self, task, ctx, runner):
        return {"agent_name": "historical_incident", "status": "done", "summary": 3}


class WrongNameAgent(SpecialistAgent):
    name = "historical_incident"

    async def run(self, task, ctx, runner):
        return AgentResult(agent_name="payment_flow", status="success", summary="yanlış ad")


@pytest.mark.parametrize("agent", [InvalidOutputAgent(), WrongNameAgent()])
def test_invalid_structured_output_is_rejected(make_toolkit, agent):
    out = run(make_toolkit, "response_code_spike", agents=with_agent(agent))
    hist = next(a for a in out["agent_results"] if a["agent_name"] == "historical_incident")
    assert hist["status"] == "failed" and "Geçersiz yapılandırılmış çıktı" in hist["errors"][0]
    assert out["report"]["risk"]["severity"] == "MEDIUM"


def test_tool_outside_allow_list_is_blocked(make_toolkit):
    class Rogue(SpecialistAgent):
        name = "historical_incident"
        tools = frozenset({"search_runbooks"})

        async def run(self, task, ctx, runner):
            cw, bw = ctx.windows()
            await runner.call(self.name, self.tools, "compare_with_baseline", {"current_window": cw, "baseline_window": bw}, "x", 1)

    out = run(make_toolkit, "response_code_spike", agents=with_agent(Rogue()))
    hist = next(a for a in out["agent_results"] if a["agent_name"] == "historical_incident")
    assert hist["status"] == "failed" and "ToolNotAllowed" in hist["errors"][0]


# ---------- Bütçe ----------


def test_tool_call_budget_is_enforced(make_toolkit):
    out = run(make_toolkit, "bank_timeout", budget=ExecutionBudget(max_tool_calls=4))
    assert out["tool_calls"] <= 4
    assert events(out, "limit")
    assert any("bütçesi" in x for x in out["report"]["limitations"])


def test_round_budget_is_enforced(make_toolkit):
    out = run(make_toolkit, "reversal_anomaly", budget=ExecutionBudget(max_rounds=1))
    assert out["rounds"] == 1 and not events(out, "follow_up")


# ---------- Kanıt ve nedensellik ----------


class ClaimingAgent(SpecialistAgent):
    """Kanıtsız, uydurma referanslı ve kesin neden iddia eden bulgular üretir."""

    def __init__(self, name: str):
        self.name = name

    async def run(self, task, ctx, runner):
        valid = runner.store.ok_refs()[0]
        return AgentResult(
            agent_name=self.name, status="success", summary="iddialar", round=task.round,
            findings=[Finding(claim="BANK_B HSM arızalı.", evidence_refs=["E999"]),
                      Finding(claim="Kök neden doğrulandı: BANK_B HSM arızası.", evidence_refs=[valid])],
            hypotheses=[AgentHypothesis(text="Kesin neden HSM'dir.", evidence_refs=[valid])],
        )


def test_unsupported_and_causal_claims_are_rejected_even_if_agents_agree(make_toolkit):
    agents = default_agents()
    agents["payment_flow"] = ClaimingAgent("payment_flow")
    agents["historical_incident"] = ClaimingAgent("historical_incident")
    out = run(make_toolkit, "bank_timeout", agents=agents)
    rep = out["report"]
    rejected = rep["orchestration"]["rejected_claims"]
    assert sum(x["claim"] == "BANK_B HSM arızalı." for x in rejected) == 2  # iki agent aynı şeyi söylemesi kanıt değil
    assert any("kesin neden" in x["reason"] for x in rejected)
    assert not any("HSM arızalı" in f or "Kök neden" in f for f in rep["agent_findings"])
    assert rep["confirmed_causes"] == []
    assert not next(v for v in rep["validation"] if v["check"].startswith("Agent bulguları"))["passed"]


def test_synthesis_is_consistent_with_evidence(make_toolkit):
    out = run(make_toolkit, "latency_then_failures")
    rep = InvestigationReport(**out["report"])
    store_refs = {r["ref"] for r in out["results"] if r.get("status") == "ok"}
    for f in rep.orchestration["merged_findings"]:
        assert set(f["evidence_refs"]) <= store_refs
    assert all(v["passed"] for v in rep.validation)  # risk agent'ı = rapor riski, rapor sayıları = araç çıktıları
    assert all(" · " in f.source and f.source.startswith("E") for f in rep.facts)
    assert any("Çoklu agent yürütmesi" in line for line in out["report_markdown"].splitlines())


# ---------- LLM (sahte) ----------


class FakeStructured:
    def __init__(self, response):
        self.response = response

    def invoke(self, _prompt):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeLLM:
    def __init__(self, selection, findings=None):
        self.selection, self.findings = selection, findings or []

    def with_structured_output(self, schema):
        if schema.__name__ == "AgentSelection":
            return FakeStructured(self.selection)
        return FakeStructured({"findings": self.findings})


def test_llm_selection_is_validated_and_findings_checked(make_toolkit):
    llm = FakeLLM({"agents": ["anomaly_detection", "payment_flow"], "rationale": "POS akışı"},
                  findings=["BANK_B timeout oranı %14.22.", "Tam 424242 işlem kayboldu."])
    out = run(make_toolkit, "bank_timeout", llm=llm)
    assert events(out, "plan")[0]["agents"] == ["anomaly_detection", "payment_flow"]
    # Başarılı LLM seçimi hata olarak değil karar olarak izlenir
    assert any(e["kind"] == "decision" and "LLM agent seçimi" in e["detail"] for e in out["trace"])
    assert not events(out, "llm_error")
    assert "BANK_B timeout oranı %14.22." in out["report"]["agent_findings"]
    assert all("424242" not in f for f in out["report"]["agent_findings"])

    bad = FakeLLM({"agents": ["root_cause_agent"]})
    out = run(make_toolkit, "bank_timeout", llm=bad)
    assert events(out, "plan")[0]["agents"] == ["anomaly_detection"]
    assert any("geçersiz" in e["detail"] for e in out["trace"] if e["kind"] in ("llm_error", "info"))


def test_llm_unavailable_keeps_deterministic_flow(make_toolkit):
    llm = FakeLLM(ConnectionError("ollama kapalı"), findings=None)
    llm.with_structured_output = lambda schema: FakeStructured(ConnectionError("ollama kapalı"))
    out = run(make_toolkit, "bank_timeout", llm=llm)
    assert out["report"]["risk"]["severity"] == "HIGH"
    assert any(e["kind"] == "llm_error" for e in out["trace"])


# ---------- Uçtan uca: iki farklı senaryo ----------


def test_end_to_end_two_scenarios_produce_traceable_reports(make_toolkit):
    timeout = run(make_toolkit, "bank_timeout")
    reversal = run(make_toolkit, "reversal_anomaly")
    for out in (timeout, reversal):
        rep = out["report"]
        assert len({a["agent_name"] for a in out["agent_results"]}) >= 3
        assert rep["agent_runs"] and all(a["evidence_refs"] for a in rep["agent_runs"] if a["agent"] != "impact_risk")
        assert all(v["passed"] for v in rep["validation"])
    assert timeout["report"]["risk"]["affected_scope"].get("bank_id") == "BANK_B"
    assert events(timeout, "parallel") and timeout["rounds"] == 1
    assert reversal["rounds"] == 2 and reversal["report"]["risk"]["affected_scope"].get("merchant_id") == "M-007"
    # Takip turundaki dar kapsam (M-007) raporun veri kapsamını ve kanıt kalitesini değiştirmez
    assert reversal["report"]["risk"]["evidence_quality"] == "sufficient"
    assert reversal["report"]["data_coverage"]["primary_transactions"] > 1000
    # Geçmiş olay benzer ama kapsamı farklı: çelişki olarak kayıtlı
    assert any(c["kind"] == "incident_scope" for c in reversal["report"]["conflicts"])
    assert AnomalyDetectionAgent.tools.isdisjoint({"search_runbooks", "analyze_reversals"})


# ---------- Çalışma zamanı ve orkestratör dalları ----------


def test_transient_tool_timeout_is_retried_in_runtime(make_toolkit):
    out = run(make_toolkit, "bank_timeout", faults={"analyze_reversals": "timeout_once"})
    entry = next(r for r in out["results"] if r["tool"] == "analyze_reversals")
    assert entry["status"] == "ok" and entry["attempts"] == 2


def test_slow_tool_exceeds_tool_timeout_and_is_reported(make_toolkit):
    budget = ExecutionBudget(tool_timeout_s=0.8, retries=0)  # yavaş araç 1.0 sn uyur
    out = run(make_toolkit, "bank_timeout", faults={"search_runbooks": "slow_once"}, budget=budget)
    entry = next(r for r in out["results"] if r["tool"] == "search_runbooks")
    assert entry["status"] == "error" and entry["error_type"] == "transient"


class BadArgsAgent(SpecialistAgent):
    name = "historical_incident"
    tools = frozenset({"search_runbooks"})

    async def run(self, task, ctx, runner):
        _, ref = await runner.call(self.name, self.tools, "search_runbooks", {"query": "x" * 400}, "geçersiz argüman testi", task.round)
        return AgentResult(agent_name=self.name, status="partial", summary="geçersiz argüman", round=task.round,
                           errors=[runner.store.get(ref)["error"]], evidence_references=[ref])


def test_invalid_tool_arguments_are_recorded_without_crashing(make_toolkit):
    out = run(make_toolkit, "response_code_spike", agents=with_agent(BadArgsAgent()))
    entry = next(r for r in out["results"] if r.get("agent") == "historical_incident")
    assert entry["status"] == "error" and entry["error_type"] == "invalid_args"
    assert out["report"]["status"] == "incomplete"


def test_baseline_failure_skips_dependent_agents(make_toolkit):
    out = run(make_toolkit, "bank_timeout", faults={"compare_with_baseline": "error"})
    skipped = {e["agent"]: e["detail"] for e in events(out, "skip")}
    assert "baseline" in skipped["historical_incident"] and "baseline" in skipped["payment_flow"]
    rep = out["report"]
    assert rep["status"] == "incomplete" and rep["risk"]["severity"] == "INDETERMINATE"
    anomaly = next(a for a in out["agent_results"] if a["agent_name"] == "anomaly_detection")
    assert anomaly["status"] == "failed"


def test_follow_up_blocked_when_budget_is_exhausted(make_toolkit):
    first = run(make_toolkit, "reversal_anomaly")
    round1_calls = sum(1 for r in first["results"] if r.get("round") == 1 and r.get("step") is not None)
    out = run(make_toolkit, "reversal_anomaly", budget=ExecutionBudget(max_tool_calls=round1_calls))
    assert out["rounds"] == 1
    assert any("bütçe" in e["detail"] for e in events(out, "limit"))


def test_deterministic_agent_selection_for_all_scenarios(make_toolkit):
    """Her senaryoda en az anomali ve risk agent'ı çalışır; gereksiz agent'lar gerekçesiyle atlanır."""
    from src.data_generator import SCENARIOS

    for s in SCENARIOS:
        out = run(make_toolkit, s)
        ran = {a for a, _ in agents_run(out)}
        skipped = {e["agent"] for e in events(out, "skip")}
        assert {"anomaly_detection", "impact_risk"} <= ran, s
        assert not ran & skipped, s
        assert ran | skipped >= {"payment_flow", "historical_incident"}, s


def test_risk_does_not_depend_on_agent_execution_order(make_toolkit):
    """Ödeme akışı önce tüm kapsamda, sonra takipte BANK_B'de çalışsa da risk aynı kanıta (inceleme kapsamı) dayanır."""
    llm = FakeLLM({"agents": ["anomaly_detection", "payment_flow"], "rationale": "POS"})
    out = run(make_toolkit, "bank_timeout", llm=llm)
    scopes = [r["args"]["filters"] for r in out["results"] if r["tool"] == "analyze_reversals"]
    assert len(scopes) == 2 and scopes[0] != scopes[1]  # tüm kapsam + BANK_B takibi
    risk_runs = [r["output"]["score"] for r in out["results"] if r["tool"] == "assess_impact"]
    assert len(set(risk_runs)) == 1 and risk_runs[-1] == out["report"]["risk"]["score"]
