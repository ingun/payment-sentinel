"""Agent orkestrasyon testleri. Gerçek LLM çağrısı yapılmaz; senaryolu sahte LLM kullanılır."""

import pytest
from langchain_core.messages import AIMessage

from src.agent import InvestigationAgent
from src.config import MAX_TOOL_CALLS
from tests.conftest import BASELINE, CURRENT, REQUEST


class FakeStructured:
    def __init__(self, scope: dict):
        self.scope = scope

    def invoke(self, _prompt):
        return {"in_scope": True, "window_minutes": None, "service": None, "error_code": None, "is_follow_up": False, **self.scope}


class FakeLLM:
    """Önceden yazılmış yanıtları sırayla döner; Exception öğeleri fırlatılır."""

    def __init__(self, script: list, scope: dict | None = None):
        self.script = list(script)
        self.scope = scope or {}
        self.calls = 0

    def bind_tools(self, _tools):
        return self

    def with_structured_output(self, _schema):
        return FakeStructured(self.scope)

    def invoke(self, messages):
        if isinstance(messages, str):  # planner
            return AIMessage(content="1. Metrikleri hesapla\n2. Baseline ile karşılaştır\n3. Paternleri analiz et")
        self.calls += 1
        item = self.script.pop(0) if self.script else AIMessage(content="Tamamlandı.")
        if isinstance(item, Exception):
            raise item
        return item


def call(name: str, args: dict, i: int = 0) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"t{i}-{name}"}])


WINDOWS = {"current_window": CURRENT, "baseline_window": BASELINE}


def tools_called(state) -> list[str]:
    return [r["tool"] for r in state["results"]]


@pytest.mark.parametrize("scenario,level", [("normal", "LOW"), ("error_spike", "HIGH"), ("volume_drop", "MEDIUM")])
def test_deterministic_end_to_end(make_toolkit, scenario, level):
    state = InvestigationAgent(make_toolkit(scenario)).run(REQUEST, thread_id=scenario)
    assert state["report"]["risk"]["level"] == level
    assert state["mode"] == "deterministic"
    assert state["plan"] and any(t["kind"] == "validation" for t in state["trace"])
    assert tools_called(state)[:3] == ["calculate_payment_metrics", "compare_time_windows", "analyze_error_patterns"]
    if scenario != "normal":
        assert {"search_payment_logs", "search_similar_incidents", "search_runbooks"} <= set(tools_called(state))
    assert state["report"]["confirmed_root_cause"] is None


def test_transient_tool_failure_is_retried(make_toolkit):
    state = InvestigationAgent(make_toolkit(faults={"analyze_error_patterns": "timeout_once"})).run(REQUEST, thread_id="t")
    entry = next(r for r in state["results"] if r["tool"] == "analyze_error_patterns")
    assert entry["status"] == "ok" and entry["attempts"] == 2


def test_tool_timeout_is_enforced(make_toolkit):
    agent = InvestigationAgent(make_toolkit(faults={"compare_time_windows": "slow_once"}), tool_timeout_s=0.2)
    entry = next(r for r in agent.run(REQUEST, thread_id="t")["results"] if r["tool"] == "compare_time_windows")
    assert entry["status"] == "ok" and entry["attempts"] == 2


def test_permanent_tool_failure_is_reported_as_limitation(make_toolkit):
    state = InvestigationAgent(make_toolkit(faults={"search_runbooks": "error"})).run(REQUEST, thread_id="t")
    report = state["report"]
    assert report["risk"]["level"] == "HIGH" and report["risk"]["confidence"] == "LOW"
    assert any("search_runbooks" in x for x in report["limitations"])
    assert sum(1 for r in state["results"] if r["tool"] == "search_runbooks") == 1  # kalıcı hata tekrar denenmez


def test_missing_core_evidence_gives_indeterminate_risk(make_toolkit):
    state = InvestigationAgent(make_toolkit(faults={"compare_time_windows": "error"})).run(REQUEST, thread_id="t")
    assert state["report"]["risk"]["level"] == "INDETERMINATE"
    assert "compare_time_windows" in state["missing"]


def test_llm_selects_tools_and_findings_are_validated(make_toolkit):
    script = [
        call("calculate_payment_metrics", {"start_time": CURRENT["start"], "end_time": CURRENT["end"]}, 1),
        call("compare_time_windows", WINDOWS, 2),
        call("analyze_error_patterns", WINDOWS, 3),
        call("search_similar_incidents", WINDOWS, 4),
        call("search_runbooks", {"query": "PROVIDER_TIMEOUT provider-connector"}, 5),
        call("generate_incident_report", {"findings": ["Hata oranı %14.67 oldu.", "Etkilenen işlem 6543.21 oldu."]}, 6),
    ]
    state = InvestigationAgent(make_toolkit(), FakeLLM(script)).run(REQUEST, thread_id="llm")
    assert state["mode"] == "llm"
    assert state["report"]["agent_findings"] == ["Hata oranı %14.67 oldu."]
    assert any(v["check"] == "Agent bulgusu reddedildi" for v in state["report"]["validation"])
    assert any(t["kind"] == "decision" and "LLM araç seçti" in t["detail"] for t in state["trace"])


def test_tool_call_limit_prevents_infinite_loop(make_toolkit):
    loop = [call("calculate_payment_metrics", {"start_time": CURRENT["start"], "end_time": CURRENT["end"]}, i) for i in range(40)]
    state = InvestigationAgent(make_toolkit(), FakeLLM(loop)).run(REQUEST, thread_id="loop")
    assert len(state["results"]) == MAX_TOOL_CALLS
    assert state["report"] is not None
    assert any("sonucu alınamadı" in x for x in state["report"]["limitations"])


def test_llm_failure_falls_back_to_deterministic(make_toolkit):
    state = InvestigationAgent(make_toolkit(), FakeLLM([RuntimeError("model down")])).run(REQUEST, thread_id="f")
    assert state["mode"] == "deterministic"
    assert any(t["kind"] == "llm_error" for t in state["trace"])
    assert state["report"]["risk"]["level"] == "HIGH"


def test_llm_that_skips_tools_is_nudged_then_completed(make_toolkit):
    lazy = [AIMessage(content="Sorun yok."), AIMessage(content="Hâlâ sorun yok.")]
    state = InvestigationAgent(make_toolkit(), FakeLLM(lazy)).run(REQUEST, thread_id="lazy")
    assert any(t["kind"] == "retry" for t in state["trace"])
    assert state["missing"] == []
    assert state["report"]["risk"]["level"] == "HIGH"


def test_hallucinated_llm_scope_is_dropped(make_toolkit):
    llm = FakeLLM([RuntimeError("stop")], scope={"service": "payment-orchestrator", "error_code": "CARD_DECLINED"})
    state = InvestigationAgent(make_toolkit(), llm).run(REQUEST, thread_id="h")
    assert state["scope"]["service"] is None and state["scope"]["error_code"] is None


def test_ambiguous_request_uses_documented_defaults(make_toolkit):
    state = InvestigationAgent(make_toolkit()).run("Bir sorun var gibi, kontrol eder misin?", thread_id="a")
    assert state["scope"]["window_minutes"] == 30
    assert any("açıkça belirtilmedi" in a for a in state["assumptions"])


def test_window_from_text(make_toolkit):
    state = InvestigationAgent(make_toolkit()).run("Son 1 saatteki ödeme hatalarını incele", thread_id="w")
    assert state["scope"]["window_minutes"] == 60


def test_follow_up_uses_memory(make_toolkit):
    agent = InvestigationAgent(make_toolkit("latency_degradation"))
    agent.run("Son 45 dakikadaki ödeme hatalarını araştır", thread_id="m")
    state = agent.run("Şimdi sadece acquirer-gateway servisine bak", thread_id="m")
    assert state["scope"]["window_minutes"] == 45
    assert state["scope"]["service"] == "acquirer-gateway"
    assert len(state["history"]) == 2


def test_off_topic_request_is_declined(make_toolkit):
    state = InvestigationAgent(make_toolkit()).run("Bana bir şiir yaz", thread_id="o")
    assert state["report"] is None and "kapsamında" in state["message"]
