"""
Payment Sentinel: ödeme operasyonu inceleme arayüzü (Streamlit).

Çalıştırma: streamlit run app.py
Arayüz iş kuralı içermez; tüm eşikler, tespitler ve risk hesapları src/ altındaki deterministik modüllerdedir.
"""

import json
import sys
import uuid
from datetime import datetime, time, timezone

import pandas as pd
import streamlit as st
from streamlit import runtime

# "python app.py" (ör. VS Code Run butonu) ile başlatılırsa kendini "streamlit run" ile yeniden başlatır
if __name__ == "__main__" and not runtime.exists():
    from streamlit.web import cli as stcli

    sys.argv = ["streamlit", "run", __file__, *sys.argv[1:]]
    sys.exit(stcli.main())
import src  # noqa: E402,F401  (langgraph'tan önce: engellenen uuid_utils DLL'i için yedek)
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from src import config
from src.agent import InvestigationAgent
from src.data_generator import SCENARIO_LABELS, SCENARIOS
from src.domain import BANKS, CHANNELS, MERCHANTS, PAYMENT_METHODS, SERVICES
from src.llm import get_llm
from src.multiagent import ExecutionBudget, Orchestrator
from src.report import DETECTOR_LABELS, DIMENSION_LABELS
from src.repositories import JsonIncidentRepository, QueryFilter, create_repository
from src.retriever import RunbookRetriever
from src.tools import PaymentToolkit
from src.validation import find_baseline, parse_timestamp, to_iso

config.setup_logging()
st.set_page_config(page_title="Payment Sentinel", page_icon=":material/shield:", layout="wide")

EXAMPLE_REQUESTS = [
    "Son 1 saatteki POS başarısızlıklarındaki artışı araştır.",
    "Son 1 saatte red (decline) oranları neden arttı? Belirli bir bankaya mı özgü?",
    "Son 1 saatte gecikme ve timeout'lar arasında ilişki var mı?",
    "Son 1 saatte reversal dağılımı değişti mi? Mutabakat riski var mı?",
    "Son 1 saatteki ödeme hatalarını araştır; bir deployment ile ilişkili olabilir mi?",
    "Bu sabah trafikte olağandışı bir şey var mı?",
    "Şimdi sadece BANK_B'ye bak.",
]
FAULT_OPTIONS = {
    "Yok": {},
    "breakdown_by_dimension: kalıcı hata (alternatif araç)": {"breakdown_by_dimension": "error"},
    "analyze_latency_timeouts: geçici zaman aşımı (1 kez)": {"analyze_latency_timeouts": "timeout_once"},
    "search_runbooks: kalıcı hata": {"search_runbooks": "error"},
    "compare_with_baseline: kalıcı hata": {"compare_with_baseline": "error"},
}
FAULT_PARAMS = {"breakdown_error": 1, "timeout": 2, "runbook_error": 3, "baseline_error": 4}
TIME_PRESETS = ["Talepten / varsayılan", "Son 30 dk", "Son 60 dk", "Bu sabah", "Özel aralık"]
SEVERITY_ALERT = {"HIGH": st.error, "MEDIUM": st.warning, "LOW": st.success, "INDETERMINATE": st.info}
ALL = "tümü"


@st.cache_resource
def load_retriever() -> RunbookRetriever:
    return RunbookRetriever()


@st.cache_resource
def load_incidents() -> JsonIncidentRepository:
    return JsonIncidentRepository()


@st.cache_resource(max_entries=10)
def load_repository(scenario: str):
    return create_repository(config.DATA_SOURCE, scenario)


@st.cache_resource(ttl=60)
def load_llm(provider: str):
    return get_llm(provider)


def reset_session() -> None:
    st.session_state.thread_id = uuid.uuid4().hex
    st.session_state.checkpointer = InMemorySaver()
    st.session_state.result = None
    st.session_state.ma_history = []


AGENT_ICONS = {"orchestrator": ":material/account_tree:", "anomaly_detection": ":material/monitoring:",
               "payment_flow": ":material/sync_alt:", "historical_incident": ":material/history:",
               "impact_risk": ":material/gpp_maybe:", "synthesizer": ":material/merge:"}
STATUS_ICONS = {"success": ":material/check_circle:", "partial": ":material/warning:", "insufficient_data": ":material/help:",
                "failed": ":material/error:"}


def render_event(container, event: dict) -> None:
    """Çoklu agent olayını canlı ilerleme kutusuna yazar (eylem, karar, kanıt; iç muhakeme yok)."""
    node, kind = event.get("node", ""), event["kind"]
    icon = AGENT_ICONS.get(node, ":material/chevron_right:")
    if kind == "tool":
        mark = ":material/check:" if event.get("status") == "ok" else ":material/close:"
        container.markdown(f"&nbsp;&nbsp;&nbsp;{mark} `{event['ref']}` **{node}** → `{event['tool']}` ({event['scope']})  \n"
                           f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{event.get('summary') or event.get('error', '')}")
    elif kind == "agent_start":
        container.markdown(f"{icon} **{node}** başladı (tur {event['round']}): {event['detail']}")
    elif kind == "agent_end":
        container.markdown(f"{STATUS_ICONS.get(event['status'], '')} **{node}** bitti · {event['status']} · {event['elapsed_ms']} ms · "
                           f"{event['tool_calls']} araç: {event['detail']}")
    elif kind in ("plan", "parallel", "skip", "follow_up", "conflict", "synthesis", "limit", "llm_error", "decision", "info"):
        container.caption(f"{icon} {node} · {event['detail']}")


def pct(rate) -> str:
    return "-" if rate is None else f"%{rate * 100:.2f}"


def latest(result: dict, tool: str) -> dict | None:
    return next((r["output"] for r in reversed(result["results"]) if r["tool"] == tool and r.get("status") == "ok"), None)


def all_outputs(result: dict, tool: str) -> list[dict]:
    return [r["output"] for r in result["results"] if r["tool"] == tool and r.get("status") == "ok"]


# ---------- Kenar çubuğu ----------

params = st.query_params
with st.sidebar:
    st.header("İnceleme ayarları")
    scenarios = list(SCENARIOS)
    default_scenario = params.get("scenario", "bank_timeout")
    scenario = st.selectbox("Sentetik senaryo", scenarios, index=scenarios.index(default_scenario) if default_scenario in scenarios else 2,
                            format_func=lambda s: f"{s} · {SCENARIOS[s]}", disabled=config.DATA_SOURCE == "elasticsearch")
    providers = ["ollama", "gemini", "none"]
    default_provider = params.get("provider", config.LLM_PROVIDER)
    provider = st.segmented_control("LLM sağlayıcı", providers, default=default_provider if default_provider in providers else "none",
                                    required=True, help="none = deterministik politika (LLM yok)")
    architectures = ["Çoklu agent", "Tek agent (LangGraph)"]
    default_arch = architectures[1] if params.get("arch") == "single" else architectures[0]
    architecture = st.segmented_control("Mimari", architectures, default=default_arch, required=True,
                                        help="Çoklu agent: orkestratör + uzman agent'lar + sentezleyici. Tek agent: LangGraph döngüsü.")
    with st.expander("Yürütme bütçesi (çoklu agent)", icon=":material/tune:"):
        defaults = ExecutionBudget()
        max_rounds = st.number_input("En fazla tur", 1, 5, defaults.max_rounds)
        max_tool_calls = st.number_input("En fazla araç çağrısı", 1, 200, defaults.max_tool_calls)
        max_parallel = st.number_input("Eşzamanlı agent", 1, 8, defaults.max_parallel)
        agent_timeout = st.number_input("Agent zaman aşımı (sn)", 1.0, 300.0, defaults.agent_timeout_s)

    st.subheader("Zaman aralığı")
    preset = st.selectbox("Pencere", TIME_PRESETS, index=TIME_PRESETS.index(params.get("preset", TIME_PRESETS[0])) if params.get("preset") in TIME_PRESETS else 0)
    custom_start = custom_end = None
    if preset == "Özel aralık":
        c1, c2 = st.columns(2)
        custom_start = c1.time_input("Başlangıç (UTC)", time(11, 0), step=300)
        custom_end = c2.time_input("Bitiş (UTC)", time(12, 0), step=300)
    baseline_choice = st.segmented_control("Baseline", ["Önceki pencere", "Dünün aynı penceresi"], default="Önceki pencere", required=True)
    baseline_minutes = st.number_input("Önceki pencere uzunluğu (dk)", min_value=30, max_value=360, value=config.DEFAULT_BASELINE_MIN, step=30,
                                       disabled=baseline_choice != "Önceki pencere")

    st.subheader("Kapsam filtreleri")
    bank = st.selectbox("Banka", [ALL, *BANKS])
    merchant = st.selectbox("Üye işyeri", [ALL, *MERCHANTS])
    channel = st.selectbox("Kanal", [ALL, *CHANNELS])
    method = st.selectbox("Ödeme yöntemi", [ALL, *PAYMENT_METHODS])
    service = st.selectbox("Servis", [ALL, *SERVICES])

    st.subheader("Dayanıklılık testi")
    fault_keys = list(FAULT_OPTIONS)
    fault_label = st.selectbox("Araç hatası simülasyonu", fault_keys, index=FAULT_PARAMS.get(params.get("fault", ""), 0))
    if st.button("Yeni oturum (hafızayı temizle)", icon=":material/restart_alt:", width="stretch"):
        reset_session()
    st.caption(f"Veri kaynağı: `{config.DATA_SOURCE}` · Araç çağrısı sınırı: tek agent {config.MAX_TOOL_CALLS}, çoklu agent {int(max_tool_calls)} · "
               f"Araç zaman aşımı: {config.TOOL_TIMEOUT_S:.0f} sn · Salt okunur")

key = f"{scenario}:{provider}:{architecture}"
if st.session_state.get("session_key") != key:
    st.session_state.session_key = key
    reset_session()

# ---------- Başlık ----------

st.title("Payment Sentinel")
st.caption("Ödeme operasyonu inceleme agent'ı: anomali tespiti, segment kırılımı, geçmiş olaylar, runbook ve kanıta dayalı rapor")
st.warning("Bu demo **tamamen sentetik** ödeme olaylarıyla çalışır. Gösterilen değerler üretim ölçümü değildir. "
           "Sistem salt okunurdur: işlem, iade, reversal veya engelleme yapmaz.", icon=":material/science:", title="Sentetik veri")

try:
    repository = load_repository(scenario)
    bounds = repository.time_bounds()
except Exception as exc:  # veri kaynağı erişilemez
    st.error(f"{type(exc).__name__}: {exc}", title="Veri kaynağı açılamadı", icon=":material/error:")
    st.stop()

llm, llm_status = load_llm(provider)
if llm_status.available:
    st.info(f"Talep yorumlama, plan ve araç seçimi LLM ({llm_status.provider}/{llm_status.model}) tarafından yapılır; "
            "sayılar ve risk her zaman deterministik araçlardan gelir.", icon=":material/smart_toy:", title="Agent modu: LLM")
else:
    st.info(f"{llm_status.message} Araçlar kanıta dayalı deterministik politikayla seçilir; sıra ara sonuçlara göre değişir.",
            icon=":material/account_tree:", title="Agent modu: deterministik politika")
if bounds:
    st.caption(f"Veri aralığı: {bounds[0]:%Y-%m-%d %H:%M} – {bounds[1]:%Y-%m-%d %H:%M} UTC (D-1 ve D, 06:00-12:00) · "
               "'Şimdi' = son kayıt zamanı")

if "request_text" not in st.session_state:
    st.session_state.request_text = SCENARIO_LABELS[scenario]["request"]
if st.session_state.get("request_for") != scenario:
    st.session_state.request_for = scenario
    st.session_state.request_text = SCENARIO_LABELS[scenario]["request"]
example = st.selectbox("Örnek talepler", ["—", *EXAMPLE_REQUESTS])
if example != "—" and st.session_state.get("last_example") != example:
    st.session_state.request_text = example
st.session_state.last_example = example
request = st.text_area("İnceleme talebi", key="request_text", height=80)


def build_ui_scope() -> dict:
    filters = {k: v for k, v in {"bank_id": bank, "merchant_id": merchant, "channel": channel, "payment_method": method,
                                  "service_name": service}.items() if v != ALL}
    scope = {"filters": filters, "baseline_minutes": int(baseline_minutes),
             "baseline_mode": "d1" if baseline_choice == "Dünün aynı penceresi" else "previous"}
    if preset in ("Son 30 dk", "Son 60 dk"):
        scope["window_minutes"] = 30 if preset == "Son 30 dk" else 60
    elif bounds and preset in ("Bu sabah", "Özel aralık"):
        day = bounds[1].date()
        start = time(6, 0) if preset == "Bu sabah" else custom_start
        end = custom_end if preset == "Özel aralık" else None
        scope["start"] = to_iso(datetime.combine(day, start, tzinfo=timezone.utc))
        scope["end"] = to_iso(datetime.combine(day, end, tzinfo=timezone.utc)) if end else to_iso(bounds[1].replace(second=0, microsecond=0))
        if preset == "Bu sabah":
            scope["baseline_mode"] = "d1"
    return scope


def run_multi_agent(toolkit: PaymentToolkit) -> None:
    budget = ExecutionBudget(max_rounds=int(max_rounds), max_tool_calls=int(max_tool_calls), max_parallel=int(max_parallel),
                             agent_timeout_s=float(agent_timeout), tool_timeout_s=config.TOOL_TIMEOUT_S)
    orchestrator = Orchestrator(toolkit, llm if llm_status.available else None, budget=budget)
    history = st.session_state.get("ma_history", [])
    with st.status("Orkestratör uzman agent'ları yürütüyor…", expanded=True) as status:
        try:
            out = orchestrator.run(request, build_ui_scope(), history[-1]["scope"] if history else None,
                                   on_event=lambda e: render_event(status, e))
            if out.get("report"):
                rep = out["report"]
                history.append({"request": request, "scope": out["scope"], "severity": rep["risk"]["severity"],
                                "detected": sorted({d["detector"] for d in rep["detections"]}), "at": rep["generated_at"]})
                st.session_state.ma_history = history[-5:]
            out["history"] = st.session_state.ma_history
            st.session_state.result = out
            status.update(label=f"İnceleme tamamlandı · {out.get('rounds', 0)} tur · {out.get('tool_calls', 0)} araç çağrısı",
                          state="complete", expanded=False)
        except Exception as exc:  # beklenmeyen hata: kullanıcıya anlaşılır mesaj
            status.update(label="İnceleme başarısız", state="error")
            st.error(f"{type(exc).__name__}: {exc}", title="İnceleme tamamlanamadı", icon=":material/error:")


clicked = st.button("İncelemeyi başlat", type="primary", icon=":material/search:")
if clicked and architecture == "Çoklu agent":
    run_multi_agent(PaymentToolkit(repository, load_retriever(), load_incidents(), faults=FAULT_OPTIONS[fault_label]))
elif clicked:
    toolkit = PaymentToolkit(repository, load_retriever(), load_incidents(), faults=FAULT_OPTIONS[fault_label])
    agent = InvestigationAgent(toolkit, llm if llm_status.available else None, checkpointer=st.session_state.checkpointer)
    with st.status("Agent inceleme yapıyor…", expanded=True) as status:
        seen = 0
        try:
            for node, trace in agent.stream(request, build_ui_scope(), st.session_state.thread_id):
                for event in trace[seen:]:
                    if event["kind"] == "tool":
                        icon = ":material/check_circle:" if event["status"] == "ok" else ":material/error:"
                        status.markdown(f"{icon} **{event['step']}. `{event['tool']}`** · kapsam: {event['scope']}  \n"
                                        f"{event.get('summary') or event.get('error', '')}")
                    elif event["kind"] == "decision" and event.get("tool"):
                        status.caption(f"Karar → `{event['tool']}`: {event['detail']}")
                    elif event["kind"] in ("scope", "validation", "retry", "llm_error", "report", "info"):
                        status.caption(f"`{node}` · {event['detail']}")
                seen = max(seen, len(trace))
            st.session_state.result = agent.final_state(st.session_state.thread_id)
            status.update(label="İnceleme tamamlandı", state="complete", expanded=False)
        except Exception as exc:  # beklenmeyen hata: kullanıcıya anlaşılır mesaj
            status.update(label="İnceleme başarısız", state="error")
            st.error(f"{type(exc).__name__}: {exc}", title="İnceleme tamamlanamadı", icon=":material/error:")

result = st.session_state.get("result")
if not result:
    st.info("Bir senaryo seçip **İncelemeyi başlat** butonuna basın.", icon=":material/arrow_upward:")
    st.stop()
if not result.get("report"):
    st.warning(result.get("message") or "Rapor üretilemedi.", icon=":material/block:", title="İnceleme yapılmadı")
    st.stop()

report = result["report"]
scope = report["scope"]
risk = report["risk"]
baseline = find_baseline(result["results"], scope)  # takip turlarındaki dar kapsamlı karşılaştırmalar değil, inceleme kapsamı

# ---------- Özet şeridi ----------

st.subheader(report["title"])
filters_text = ", ".join(f"{k}={v}" for k, v in scope["filters"].items()) or "tüm trafik"
st.caption(f"Mod: {report['mode']} · Baseline: {scope['baseline_start'][:16].replace('T', ' ')} – {scope['baseline_end'][11:16]} UTC "
           f"({'dünün aynı penceresi' if scope['baseline_mode'] == 'd1' else 'önceki pencere'}) · Kapsam: {filters_text}")

if report["status"] == "insufficient_evidence":
    st.info("Örneklem veya veri kapsamı güvenilir bir sonuç için yeterli değil. Anomali iddia edilmedi.",
            icon=":material/help:", title="Kanıt yetersiz")
elif report["status"] == "incomplete":
    st.warning("Bazı kanıtlar alınamadı; ayrıntılar sınırlamalar bölümünde ve işlem izlerinde.",
               icon=":material/report:", title="İnceleme eksik")

if baseline:
    cur, base, d = baseline["current"], baseline["baseline"], baseline["deltas"]
    with st.container(horizontal=True):
        st.metric("İşlem / dk", baseline["volume"]["current_per_min"],
                  f"{baseline['volume']['change_pct']:+.1f}%" if baseline["volume"]["change_pct"] is not None else None, border=True)
        st.metric("Teknik hata oranı", pct(cur["technical_rate"]), f"{d['technical_pp']:+.2f} puan" if d["technical_pp"] is not None else None,
                  delta_color="inverse", border=True)
        st.metric("Red oranı", pct(cur["decline_rate"]), f"{d['decline_pp']:+.2f} puan" if d["decline_pp"] is not None else None,
                  delta_color="inverse", border=True)
        st.metric("Timeout oranı", pct(cur["timeout_rate"]), f"{d['timeout_pp']:+.2f} puan" if d["timeout_pp"] is not None else None,
                  delta_color="inverse", border=True)
        st.metric("p95 işlem süresi", f"{cur['p95_ms']} ms" if cur["p95_ms"] is not None else "-",
                  f"{d['p95_change_pct']:+.1f}%" if d["p95_change_pct"] is not None else None, delta_color="inverse", border=True)

score = f" · skor {risk['score']}/100" if risk["score"] is not None else ""
SEVERITY_ALERT[risk["severity"]](
    f"Kanıt kalitesi: **{risk['evidence_quality']}** · güven: **{risk['confidence']}** · etkilenen işlem: ~{risk['affected_transactions']} · "
    f"parasal etki belirlenemedi. _{risk['note']}_", title=f"Risk: {risk['severity']}{score}", icon=":material/gpp_maybe:")

for line in report["executive_summary"][1:]:
    st.markdown(f"- {line}")

# ---------- Sekmeler ----------

tabs = st.tabs(["Agent'lar", "Tespitler", "Zaman serisi", "Segmentler", "Gecikme ve timeout", "Reversal", "Sürümler",
                "Benzer olaylar", "Runbook'lar", "Risk", "Gerçekler ve hipotezler", "İşlem izleri", "Rapor", "Hafıza"])

with tabs[0]:
    if not report.get("agent_runs"):
        st.caption("Bu inceleme tek agent (LangGraph) mimarisiyle yapıldı. Uzman agent dökümü için kenar çubuğunda 'Çoklu agent' seçin.")
    else:
        plan = next((t for t in result["trace"] if t["kind"] == "plan"), None)
        orch = report["orchestration"]
        with st.container(horizontal=True):
            st.metric("Tur", orch["rounds"], border=True)
            st.metric("Agent çalıştırması", len(report["agent_runs"]), border=True)
            st.metric("Araç çağrısı", result.get("tool_calls", 0), border=True)
            st.metric("Çelişki", len(report["conflicts"]), border=True)
            st.metric("Reddedilen iddia", len(orch["rejected_claims"]), border=True)
        if plan:
            st.markdown(f"**Orkestratör planı** ({plan['detail']})")
            st.markdown("\n".join(f"{i}. {s}" for i, s in enumerate(plan["steps"], 1)))
        for event in [e for e in result["trace"] if e["kind"] in ("parallel", "follow_up", "skip", "limit")]:
            icon = {"parallel": ":material/call_split:", "follow_up": ":material/replay:", "skip": ":material/skip_next:",
                    "limit": ":material/block:"}[event["kind"]]
            st.markdown(f"{icon} {event['detail']}")
        st.dataframe(pd.DataFrame([{
            "Agent": a["agent"], "Tur": a["round"], "Durum": a["status"], "Süre (ms)": a["elapsed_ms"], "Araç": a["tool_calls"],
            "Kanıt referansları": ", ".join(a["evidence_refs"]), "Özet": a["summary"], "Hata": "; ".join(a["errors"]),
        } for a in report["agent_runs"]]), hide_index=True, alt="Agent çalıştırmaları",
            column_config={"Özet": st.column_config.TextColumn(width="large")})
        st.markdown("**Birleştirilmiş bulgular** (yalnızca araç sonucuna dayananlar)")
        for f in orch["merged_findings"]:
            st.markdown(f"- {f['claim']} _({', '.join(f['evidence_refs'])} · {', '.join(f['agents'])})_")
        if report["conflicts"]:
            st.markdown("**Çelişkiler**")
            for c in report["conflicts"]:
                (st.success if c["resolved"] else st.warning)(c["description"], title=f"{c['kind']} · {'çözüldü' if c['resolved'] else 'açık'}")
        if orch["rejected_claims"]:
            st.markdown("**Reddedilen iddialar**")
            for x in orch["rejected_claims"]:
                st.markdown(f"- {x['agent']}: {x['claim'][:140]} — _{x['reason']}_")
        st.caption("Agent'lar birbirinin serbest metnini değil, paylaşılan kanıt deposunu (E1, E2…) kullanır. "
                   "Birden fazla agent'ın aynı iddiayı tekrarlaması kanıt sayılmaz.")

with tabs[1]:
    rows = [{"Durum": "tespit" if d["detected"] else ("veri yetersiz" if d["insufficient_data"] else "eşik altı"),
             "Tespit": DETECTOR_LABELS.get(d["detector"], d["detector"]), "Konu": d["subject"] or "",
             "Kapsam": ", ".join(f"{k}={v}" for k, v in d["scope"].items()) or "tümü",
             "Gözlenen": d["observed"], "Baseline": d["baseline"], "Göreli sapma %": d["rel_deviation_pct"],
             "n (güncel/baseline)": f"{d['sample_size'].get('current')}/{d['sample_size'].get('baseline')}",
             "Kanıt": d["evidence_quality"], "Tür": d["claim_type"], "Açıklama": d["evidence"], "Yöntem": d["method"]}
            for d in report["detections"] + report["evaluated"]]
    st.dataframe(pd.DataFrame(rows), hide_index=True, alt="Tespit ve değerlendirilen dedektörler tablosu",
                 column_config={"Açıklama": st.column_config.TextColumn(width="large"), "Yöntem": st.column_config.TextColumn(width="medium")})
    st.caption("Tespitler deterministik istatistiksel kurallarla hesaplanır (iki oran z-testi, Bonferroni düzeltmeli segment testi, "
               "Spearman korelasyonu). Eşikler config/thresholds.toml ile değiştirilebilir ve demo amaçlıdır.")

with tabs[2]:
    start, end = parse_timestamp(scope["baseline_start"]), parse_timestamp(scope["current_end"])
    if scope["baseline_mode"] == "d1" or (end - start).total_seconds() > 24 * 3600:
        start = parse_timestamp(scope["current_start"])
    hist = pd.DataFrame(repository.histogram(QueryFilter.make(start, end, scope["filters"]), config.BUCKET_MINUTES))
    if hist.empty or not hist["total"].sum():
        st.caption("Grafik için veri yok.")
    else:
        hist["zaman"] = pd.to_datetime(hist["bucket_start"]).dt.tz_localize(None)
        hist["teknik_hata_%"] = (hist["technical"] / hist["total"].where(hist["total"] > 0) * 100).round(2)
        hist["red_%"] = (hist["declined"] / hist["total"].where(hist["total"] > 0) * 100).round(2)
        hist = hist.set_index("zaman")
        st.caption(f"5 dakikalık dilimler (UTC). İncelenen dönem {scope['current_start'][11:16]}'da başlıyor.")
        st.line_chart(hist[["teknik_hata_%", "red_%"]], height=220, y_label="%", alt="Teknik hata ve red oranı zaman serisi")
        c1, c2 = st.columns(2)
        c1.line_chart(hist[["p95_ms"]], height=200, y_label="p95 ms", alt="p95 işlem süresi zaman serisi")
        c2.bar_chart(hist[["total"]], height=200, y_label="işlem / 5 dk", alt="İşlem hacmi zaman serisi")

with tabs[3]:
    if not report["affected_scope"]:
        st.caption("Segment kırılımı yapılmadı; baseline karşılaştırmasında gerekli sinyal yoktu.")
    for a in report["affected_scope"]:
        with st.container(border=True):
            st.markdown(f"**{DIMENSION_LABELS.get(a['dimension'], a['dimension'])} kırılımı** · metrik `{a['metric']}` · "
                        + ("izole" if a["isolated"] else "izole değil"))
            st.caption(a["verdict"])
            seg = pd.DataFrame(a["top_segments"])
            for col in ("rate", "baseline_rate", "rest_rate"):
                seg[col] = (seg[col] * 100).round(2)
            st.dataframe(seg.rename(columns={"segment": "Segment", "n": "İşlem", "rate": "Oran %", "baseline_rate": "Kendi baseline %",
                                             "rest_rate": "Diğer segmentler %", "z_vs_rest": "z (diğerlerine göre)", "excess": "Fazladan"}),
                         hide_index=True, alt=f"{a['dimension']} segment tablosu")
    if report["response_codes"]:
        st.markdown("**Olağandışı yanıt kodları**")
        codes = pd.DataFrame(report["response_codes"])[["code", "description", "status", "count", "rate", "baseline_rate", "z"]]
        codes[["rate", "baseline_rate"]] = (codes[["rate", "baseline_rate"]] * 100).round(2)
        st.dataframe(codes.rename(columns={"code": "Kod", "description": "Açıklama", "status": "Durum", "count": "Adet",
                                           "rate": "Oran %", "baseline_rate": "Baseline %"}), hide_index=True, alt="Olağandışı yanıt kodları")

with tabs[4]:
    lat = latest(result, "analyze_latency_timeouts")
    if not lat:
        st.caption("Gecikme/timeout analizi gerekmedi (timeout veya gecikme sinyali yoktu).")
    else:
        for line in report["latency_findings"]:
            st.markdown(f"- {line}")
        b = pd.DataFrame(lat["buckets"])
        if not b.empty:
            b["zaman"] = pd.to_datetime(b["bucket_start"]).dt.tz_localize(None)
            b["hata_%"] = ((b["timeouts"] + b["errors"]) / b["total"].where(b["total"] > 0) * 100).round(2)
            b = b.set_index("zaman")
            c1, c2 = st.columns(2)
            c1.line_chart(b[["p95_ms"]], height=220, y_label="p95 ms", alt="Etkilenen kapsamda p95 işlem süresi")
            c2.line_chart(b[["hata_%"]], height=220, y_label="teknik hata %", alt="Etkilenen kapsamda teknik hata oranı")

with tabs[5]:
    rev = all_outputs(result, "analyze_reversals")
    if not rev:
        st.caption("Reversal analizi gerekmedi.")
    for out in rev:
        with st.container(border=True):
            st.markdown(f"**Kapsam:** {', '.join(f'{k}={v}' for k, v in out['filters'].items()) or 'tüm trafik'}")
            cats = pd.DataFrame([{"Sınıf": k, "Güncel": out["current"]["categories"][k], "Baseline": out["baseline"]["categories"][k]}
                                 for k in out["current"]["categories"]])
            st.dataframe(cats, hide_index=True, alt="Reversal sınıfları")
            st.caption(out["detection"]["evidence"])
            rr = out["reconciliation_risk"]
            st.markdown(f"Mutabakat riski göstergesi: **{rr['level']}**")
            for ind in rr["indicators"]:
                st.markdown(f"- {ind}")
            st.caption(rr["note"])

with tabs[6]:
    versions = all_outputs(result, "compare_service_versions")
    if not versions:
        st.caption("Sürüm karşılaştırması gerekmedi (sorun belirli bir servise izole değildi).")
    for out in versions:
        st.markdown(f"**{out['service']}** · {out['detection']['evidence']}")
        v = pd.DataFrame(out["versions"])
        v["technical_rate"] = (v["technical_rate"] * 100).round(2)
        v["others_rate"] = (v["others_rate"] * 100).round(2)
        st.dataframe(v, hide_index=True, alt="Servis sürümleri karşılaştırması")
        st.dataframe(pd.DataFrame(out["deployments"]), hide_index=True, alt="Sürümlerin ilk görülme zamanları")
        st.caption("Sürüm ilişkisi bir hipotezdir; değişiklik kayıtlarıyla doğrulanmalıdır.")

with tabs[7]:
    if not report["similar_incidents"]:
        st.caption("Eşleşen geçmiş olay yok; eşleşme uydurulmadı.")
    for m in report["similar_incidents"]:
        with st.container(border=True):
            st.markdown(f"**{m['incident_id']}** · benzerlik **{m['similarity']}** · {m['service']} · kodlar: {', '.join(m['response_codes']) or '-'}")
            st.markdown(m["summary"])
            st.markdown(f"- Neden benzer: {'; '.join(m['why_similar']) or '-'}\n- Farklar: {'; '.join(m['differences']) or '-'}\n"
                        f"- O olayda doğrulanan faktörler: {'; '.join(m['established_factors']) or 'yok'}\n"
                        f"- Doğrulanmamış faktörler: {'; '.join(m['unconfirmed_factors']) or 'yok'}\n"
                        f"- Çözüm: {'; '.join(m['resolution_steps'])} · Sonuç: {m['outcome']}")
    st.info(report["similar_incidents_note"], icon=":material/info:")

with tabs[8]:
    runbooks = latest(result, "search_runbooks")
    if not runbooks:
        st.caption("Runbook araması yapılmadı veya başarısız oldu.")
    else:
        st.caption(f"Sorgu: `{runbooks['query']}` · {runbooks['message']}")
        for hit in runbooks["hits"]:
            with st.expander(f"{hit['source']} › {hit['section'].split(' › ')[-1]} (skor {hit['score']})"):
                if hit["flagged"]:
                    st.warning("Bu parçada doküman içi talimat tespit edildi ve çıkarıldı; içerik güvenilmeyen veri olarak ele alınır.")
                st.text(hit["text"])
    st.markdown("**Önerilen sonraki adımlar**")
    for i, rec in enumerate(report["recommendations"], 1):
        st.markdown(f"{i}. {rec['text']} _(kaynak: {rec['source'] if rec['source'] != '-' else ', '.join(rec['evidence_refs'])})_")

with tabs[9]:
    if risk["factors"]:
        st.dataframe(pd.DataFrame(risk["factors"]).rename(columns={"factor": "Faktör", "value": "Değer", "points": "Puan", "explanation": "Gerekçe"}),
                     hide_index=True, alt="Risk skoru faktörleri")
    else:
        st.caption("Risk puanına katkı yapan faktör yok.")
    if risk.get("capped"):
        st.warning(risk["capped"])
    st.markdown("**Güven gerekçeleri**")
    for r in risk["confidence_reasons"]:
        st.markdown(f"- {r}")
    st.markdown(f"**Etkilenen kapsam:** {', '.join(f'{k}={v}' for k, v in risk['affected_scope'].items()) or '-'}")
    st.markdown(f"**Parasal etki:** {risk['monetary_impact']}")
    with st.expander("Skor formülü ve eşikler"):
        st.markdown(risk["formula"])
    for lim in risk["evidence_limitations"]:
        st.caption(lim)

with tabs[10]:
    st.markdown("**Doğrulanmış gerçekler (ölçülmüş)**")
    for f in report["facts"]:
        st.markdown(f"- **{f['id']}**: {f['text']} _({f['source']})_")
    st.markdown("**Hipotezler (doğrulanmadı)**")
    for h in report["hypotheses"] or [{"text": "Yok.", "evidence_refs": []}]:
        st.markdown(f"- {h['text']}" + (f" _(dayanak: {', '.join(h['evidence_refs'])})_" if h["evidence_refs"] else ""))
    st.markdown("**Doğrulanmış nedenler**")
    st.markdown("- Yok. Neden bu otomatik incelemeyle doğrulanamaz; insan incelemesi gerekir.")
    if report["agent_findings"]:
        st.markdown("**Agent bulgu özeti** _(sayılar araç çıktılarıyla doğrulandı; LLM ifadelerinin anlamı otomatik doğrulanamaz)_")
        for f in report["agent_findings"]:
            st.markdown(f"- {f}")
    st.markdown("**Sınırlamalar**")
    for lim in report["limitations"]:
        st.markdown(f"- {lim}")

with tabs[11]:
    plan = next((t for t in result["trace"] if t["kind"] == "plan"), None)
    if plan:
        st.markdown(f"**{plan['detail']}**")
        st.markdown("\n".join(f"{i}. {s}" for i, s in enumerate(plan["steps"], 1)))
    rows = [{"Adım": t.get("step", ""), "Tür": t["kind"], "Düğüm": t["node"], "Araç": t.get("tool", ""),
             "Kapsam": t.get("scope", "") if isinstance(t.get("scope"), str) else "",
             "Durum": t.get("status", "ok" if t.get("passed") else ("uyarı" if t.get("passed") is False else "")),
             "Karar gerekçesi": t.get("decision_reason", "") or (t.get("detail", "") if t["kind"] != "tool" else ""),
             "Kanıt özeti": t.get("summary") or t.get("error") or "", "Deneme": str(t.get("attempts", "")),
             "Süre (ms)": str(t.get("duration_ms", "")), "Argümanlar": json.dumps(t["args"], ensure_ascii=False) if t.get("args") else ""}
            for t in result["trace"] if t["kind"] != "plan"]
    st.dataframe(pd.DataFrame(rows), hide_index=True, height=min(40 + 35 * len(rows), 760), alt="Agent işlem izleri",
                 column_config={"Karar gerekçesi": st.column_config.TextColumn(width="large"),
                                "Kanıt özeti": st.column_config.TextColumn(width="large")})
    st.caption("Gözlemlenebilir eylemler, araçlar, kapsamlar, kanıt özetleri ve karar gerekçeleri gösterilir; modelin iç muhakemesi gösterilmez.")

with tabs[12]:
    with st.container(horizontal=True):
        st.download_button("Raporu indir (Markdown)", result["report_markdown"], file_name="investigation_report.md",
                           mime="text/markdown", icon=":material/download:")
        st.download_button("Raporu indir (JSON)", json.dumps(report, ensure_ascii=False, indent=2, default=str),
                           file_name="investigation_report.json", mime="application/json", icon=":material/data_object:")
    st.markdown(result["report_markdown"])

with tabs[13]:
    memory = "oturum geçmişi (orkestratöre önceki kapsam verilir)" if report.get("agent_runs") else "LangGraph checkpointer"
    st.caption(f"Oturum `{st.session_state.thread_id[:8]}` · {memory}. Takip soruları ('Şimdi sadece BANK_B'ye bak') önceki kapsamı kullanır.")
    for h in reversed(result.get("history", [])):
        st.markdown(f"- **{h['at'][11:19]}** · _{h['request'][:90]}_ → **{h['severity']}**, kapsam: "
                    f"{', '.join(f'{k}={v}' for k, v in h['scope'].get('filters', {}).items()) or 'tümü'}, tespitler: {', '.join(h['detected']) or 'yok'}")
