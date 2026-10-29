"""Agent orkestrasyon testleri. Gerçek LLM çağrısı yapılmaz; senaryolu sahte LLM kullanılır."""

from langchain_core.messages import AIMessage

from src.agent import FindingsSummary, InvestigationAgent, rule_based_scope
from src.config import MAX_TOOL_CALLS
from src.data_generator import SCENARIO_LABELS
from tests.conftest import BASELINE, CURRENT


class FakeStructured:
    def __init__(self, response):
        self.response = response

    def invoke(self, _prompt):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeLLM:
    """Önceden yazılmış yanıtları sırayla döner; Exception öğeleri fırlatılır."""

    def __init__(self, script: list, scope: dict | None = None, findings=None):
        self.script = list(script)
        self.scope = scope or {}
        self.findings = findings if findings is not None else []
        self.seen: list = []

    def bind_tools(self, _tools):
        return self

    def with_structured_output(self, schema):
        if schema is FindingsSummary:
            return FakeStructured(self.findings if isinstance(self.findings, Exception) else {"findings": self.findings})
        return FakeStructured({"in_scope": True, "window_minutes": None, "bank_id": None, "merchant_id": None,
                               "channel": None, "service_name": None, "is_follow_up": False, **self.scope})

    def invoke(self, messages):
        if isinstance(messages, str):  # planner
            return AIMessage(content="1. Veri kapsamını kontrol et\n2. Baseline ile karşılaştır\n3. Segment kırılımı yap")
        self.seen.append(messages)
        item = self.script.pop(0) if self.script else AIMessage(content="Tamamlandı.")
        if isinstance(item, Exception):
            raise item
        return item


def call(name: str, args: dict, i: int = 0) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"t{i}-{name}"}])


def run(make_toolkit, scenario: str, request: str | None = None, llm=None, faults=None, **kw):
    agent = InvestigationAgent(make_toolkit(scenario, faults=faults), llm, **kw)
    return agent.run(SCENARIO_LABELS[scenario]["request"] if request is None else request, thread_id="t")


def path(state: dict) -> list[str]:
    return [r["tool"] for r in state["results"]]


# ---------- Deterministik politika: ara sonuçlara göre farklı yollar ----------


def test_investigation_paths_depend_on_evidence(make_toolkit):
    paths = {s: path(run(make_toolkit, s)) for s in ("normal_baseline", "bank_timeout", "latency_then_failures",
                                                     "reversal_anomaly", "version_regression", "response_code_spike", "low_volume")}
    assert paths["normal_baseline"] == ["check_data_coverage", "compare_with_baseline", "submit_findings"]
    assert paths["low_volume"] == ["check_data_coverage", "compare_with_baseline", "submit_findings"]
    # Banka izole: servis kırılımına gerek kalmaz
    assert paths["bank_timeout"].count("breakdown_by_dimension") == 1 and "analyze_latency_timeouts" in paths["bank_timeout"]
    # Banka izole değil → servis kırılımı → sürüm karşılaştırması
    assert paths["latency_then_failures"].count("breakdown_by_dimension") == 2
    assert "compare_service_versions" in paths["version_regression"]
    assert "analyze_latency_timeouts" not in paths["version_regression"]
    assert paths["reversal_anomaly"][2] == "analyze_reversals"
    assert "breakdown_by_dimension" not in paths["response_code_spike"]
    assert len({tuple(p) for p in paths.values()}) >= 5


def test_trace_shows_actions_scope_evidence_and_reasons(make_toolkit):
    state = run(make_toolkit, "bank_timeout")
    tools = [t for t in state["trace"] if t["kind"] == "tool"]
    assert all(t["decision_reason"] and t["summary"] and t["scope"] for t in tools)
    analyze = next(t for t in tools if t["tool"] == "analyze_response_codes")
    assert "bank_id=BANK_B" in analyze["scope"]  # takip sorgusu izole bankaya daraltıldı
    assert not any("chain" in str(t).lower() for t in state["trace"])


def test_deterministic_mode_completes_without_llm(make_toolkit):
    state = run(make_toolkit, "version_regression", llm=None)
    rep = state["report"]
    assert state["mode"] == "deterministic" and rep["status"] == "complete"
    assert any(d["detector"] == "SERVICE_VERSION_DEGRADATION" for d in rep["detections"])
    assert rep["similar_incidents"][0]["incident_id"] == "INC-SYN-2026-0721"
    assert rep["runbook_sources"]


# ---------- Hata yönetimi ----------


def test_transient_timeout_is_retried(make_toolkit):
    state = run(make_toolkit, "bank_timeout", faults={"breakdown_by_dimension": "timeout_once"})
    entry = next(r for r in state["results"] if r["tool"] == "breakdown_by_dimension")
    assert entry["status"] == "ok" and entry["attempts"] == 2


def test_slow_tool_hits_timeout_and_is_retried(make_toolkit):
    state = run(make_toolkit, "normal_baseline", faults={"compare_with_baseline": "slow_once"}, tool_timeout_s=0.8)  # yavaş araç 1.0 sn uyur; gerçek araçlara yük altında pay
    entry = next(r for r in state["results"] if r["tool"] == "compare_with_baseline")
    assert entry["status"] == "ok" and entry["attempts"] == 2


def test_permanent_failure_uses_alternative_tool(make_toolkit):
    state = run(make_toolkit, "bank_timeout", faults={"breakdown_by_dimension": "error"})
    rc = [r for r in state["results"] if r["tool"] == "analyze_response_codes"]
    assert any((r["args"].get("dimension") == "bank_id") for r in rc)
    assert state["report"]["risk"]["affected_scope"].get("bank_id") == "BANK_B"
    assert state["report"]["risk"]["confidence"] != "HIGH"


def test_missing_runbook_marks_incomplete_and_lowers_confidence(make_toolkit):
    state = run(make_toolkit, "bank_timeout", faults={"search_runbooks": "error"})
    rep = state["report"]
    assert rep["status"] == "incomplete" and "search_runbooks" in state["missing"]
    assert rep["risk"]["severity"] == "HIGH" and rep["risk"]["confidence"] == "LOW"
    assert any("search_runbooks" in x for x in rep["limitations"])


def test_malformed_tool_output_is_rejected(make_toolkit):
    state = run(make_toolkit, "normal_baseline", faults={"check_data_coverage": "malformed"})
    entry = state["results"][0]
    assert entry["status"] == "error" and "Hatalı biçimli" in entry["error"]
    assert state["report"]["risk"]["severity"] == "LOW"


def test_baseline_failure_gives_incomplete_indeterminate_report(make_toolkit):
    state = run(make_toolkit, "bank_timeout", faults={"compare_with_baseline": "error"})
    rep = state["report"]
    assert rep["status"] == "incomplete" and rep["risk"]["severity"] == "INDETERMINATE"


# ---------- LLM modu ----------

WINDOWS = {"current_window": CURRENT, "baseline_window": BASELINE, "filters": {}}


def test_llm_unavailable_falls_back_to_policy(make_toolkit):
    llm = FakeLLM([ConnectionError("ollama kapalı")])
    state = run(make_toolkit, "bank_timeout", llm=llm)
    assert state["mode"] == "deterministic"
    assert any(t["kind"] == "llm_error" for t in state["trace"])
    assert state["report"]["risk"]["severity"] == "HIGH"
    assert any("LLM" in x for x in state["report"]["limitations"])


def test_llm_drives_tools_and_invalid_args_are_reported(make_toolkit):
    script = [
        call("check_data_coverage", {"window": CURRENT}, 0),
        call("compare_with_baseline", {**WINDOWS, "filters": {"bank_id": "BANK_Z!"}}, 1),  # geçersiz
        call("compare_with_baseline", WINDOWS, 2),
        call("breakdown_by_dimension", {**WINDOWS, "dimension": "bank_id"}, 3),
        call("search_similar_incidents", WINDOWS, 4),
        call("search_runbooks", {"query": "POS timeout banka"}, 5),
        call("submit_findings", {"findings": ["BANK_B'de teknik hata oranı %18.67.", "Kök neden kesinlikle HSM."]}, 6),
    ]
    llm = FakeLLM(script, scope={"window_minutes": 60})
    state = InvestigationAgent(make_toolkit("bank_timeout"), llm).run("Son 60 dakikadaki ödeme hatalarını araştır", thread_id="x")
    assert state["mode"] == "llm"
    bad = state["results"][1]
    assert bad["status"] == "error" and bad["error_type"] == "invalid_args"
    rep = state["report"]
    assert "BANK_B'de teknik hata oranı %18.67." in rep["agent_findings"]
    assert any(v["check"] == "Agent bulgusu reddedildi" for v in rep["validation"])
    # Politika önerisi LLM'e ipucu olarak gönderilir
    assert any("[politika önerisi]" in str(m.content) for msgs in llm.seen for m in msgs)


def test_llm_hallucinated_scope_is_dropped(make_toolkit):
    llm = FakeLLM([AIMessage(content="bitti")], scope={"bank_id": "BANK_D", "service_name": "ecom-gateway"})
    state = InvestigationAgent(make_toolkit("normal_baseline"), llm).run("Son 1 saatteki ödeme hatalarını kontrol et", thread_id="h")
    assert state["scope"]["filters"] == {}
    assert any(t["kind"] == "validation" and "BANK_D" in t["detail"] for t in state["trace"])


def test_llm_skipping_required_evidence_is_completed_by_verifier(make_toolkit):
    llm = FakeLLM([call("compare_with_baseline", WINDOWS, 0), AIMessage(content="bitti")])
    state = InvestigationAgent(make_toolkit("bank_timeout"), llm).run("Son 60 dakikadaki ödeme hatalarını araştır", thread_id="v")
    tools = path(state)
    assert "search_similar_incidents" in tools and "search_runbooks" in tools
    assert any(t["kind"] == "retry" for t in state["trace"])
    # Verifier hatırlatması somut bir araç çağrısı önerir
    assert any("Önerilen sonraki çağrı" in str(m.content) for msgs in llm.seen for m in msgs)


def test_llm_summarizer_findings_are_validated(make_toolkit):
    llm = FakeLLM([ConnectionError("x")], findings=["Teknik hata oranı %0.56'dan %5.17'ye çıktı.", "Tam 999999 işlem kayboldu."])
    state = run(make_toolkit, "bank_timeout", llm=llm)
    rep = state["report"]
    assert "Teknik hata oranı %0.56'dan %5.17'ye çıktı." in rep["agent_findings"]
    assert all("999999" not in f for f in rep["agent_findings"])


def test_tool_call_limit_is_enforced(make_toolkit):
    script = [call("get_payment_metrics", {"window": CURRENT}, i) for i in range(MAX_TOOL_CALLS + 5)]
    state = InvestigationAgent(make_toolkit("normal_baseline"), FakeLLM(script)).run("ödeme hatalarını kontrol et", thread_id="l")
    assert state["tool_calls"] <= MAX_TOOL_CALLS
    assert state["report"] is not None


# ---------- Talep yorumlama ve hafıza ----------


def test_out_of_scope_request_is_declined(make_toolkit):
    state = run(make_toolkit, "normal_baseline", request="Bana bir şiir yaz")
    assert state["report"] is None and "kapsamında" in state["message"]


def test_ambiguous_request_uses_documented_defaults(make_toolkit):
    state = run(make_toolkit, "bank_timeout", request="Ödemelerde bir sorun var gibi, kontrol eder misin?")
    assert state["scope"]["window_minutes"] == 60 and state["scope"]["filters"] == {}
    assert any("açıkça belirtilmedi" in a for a in state["assumptions"])
    assert state["report"]["risk"]["severity"] == "HIGH"  # belirsiz talep yine tam incelemeye dönüşür
    assert any("açıkça belirtilmedi" in a for a in state["report"]["assumptions"])


def test_request_parsing():
    s = rule_based_scope("Bu sabahki POS hatalarında B bankası için artışı araştır")
    assert s["morning"] and s["filters"] == {"channel": "POS", "bank_id": "BANK_B"}
    s = rule_based_scope("10:30-11:45 arası M-007 reversal'larını incele")
    assert s["clock_range"] == (10, 30, 11, 45) and s["filters"] == {"merchant_id": "M-007"}
    assert rule_based_scope("Son 2 saatte payment-orchestrator hataları")["window_minutes"] == 120


def test_this_morning_uses_yesterday_as_baseline(make_toolkit):
    state = run(make_toolkit, "normal_fluctuation", request="Bu sabah trafikte olağandışı bir şey var mı?")
    scope = state["scope"]
    assert scope["current_start"] == "2026-10-27T06:00:00Z" and scope["baseline_start"] == "2026-10-26T06:00:00Z"
    assert scope["baseline_mode"] == "d1"


def test_ui_scope_filters_and_custom_window(make_toolkit):
    agent = InvestigationAgent(make_toolkit("bank_timeout"))
    state = agent.run("ödeme hatalarını incele", ui_scope={"start": "2026-10-27T11:10:00Z", "end": "2026-10-27T11:50:00Z",
                                                          "filters": {"bank_id": "BANK_B"}}, thread_id="u")
    assert state["scope"]["window_minutes"] == 40 and state["scope"]["filters"] == {"bank_id": "BANK_B"}
    assert "breakdown_by_dimension" not in path(state) or all(r["args"].get("dimension") != "bank_id" for r in state["results"])


def test_follow_up_reuses_previous_scope(make_toolkit):
    agent = InvestigationAgent(make_toolkit("bank_timeout"))
    agent.run("Son 45 dakikadaki ödeme hatalarını araştır", thread_id="m")
    state = agent.run("Şimdi sadece BANK_B'ye bak", thread_id="m")
    assert state["scope"]["window_minutes"] == 45 and state["scope"]["filters"]["bank_id"] == "BANK_B"
    assert len(state["history"]) == 2
