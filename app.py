"""
Payment Sentinel: Streamlit arayüzü.

Çalıştırma: streamlit run app.py
"""

import json
import sys
import uuid

import pandas as pd
import streamlit as st
from streamlit import runtime

# "python app.py" (ör. VS Code Run butonu) ile başlatılırsa kendini "streamlit run" ile yeniden başlatır
if __name__ == "__main__" and not runtime.exists():
    from streamlit.web import cli as stcli

    sys.argv = ["streamlit", "run", __file__, *sys.argv[1:]]
    sys.exit(stcli.main())
from langgraph.checkpoint.memory import InMemorySaver

from src import config
from src.agent import InvestigationAgent
from src.data_generator import SCENARIOS
from src.datasource import QueryFilter, SyntheticDataSource, create_datasource
from src.incident_matcher import load_incidents
from src.llm import get_llm
from src.report import PATTERN_LABELS
from src.retriever import RunbookRetriever
from src.tools import PaymentToolkit
from src.validation import parse_timestamp

config.setup_logging()
st.set_page_config(page_title="Payment Sentinel", page_icon="🛡️", layout="wide")

EXAMPLE_REQUESTS = [
    "Son 30 dakikadaki ödeme başarısızlıklarını araştır, normal dönemle karşılaştır ve olası incident nedenlerini raporla.",
    "Son 1 saatte gecikme ve hata oranında anomali var mı?",
    "Bir sorun var gibi, kontrol eder misin?",
    "Şimdi sadece acquirer-gateway servisine bak.",
    "PROVIDER_TIMEOUT hatalarını incele.",
]
FAULT_OPTIONS = {
    "Yok": {},
    "analyze_error_patterns: geçici zaman aşımı (1 kez)": {"analyze_error_patterns": "timeout_once"},
    "search_runbooks: kalıcı servis hatası": {"search_runbooks": "error"},
    "compare_time_windows: kalıcı servis hatası": {"compare_time_windows": "error"},
}
RISK_STYLE = {"HIGH": ("🔴", "error"), "MEDIUM": ("🟠", "warning"), "LOW": ("🟢", "success"), "INDETERMINATE": ("⚪", "info")}


@st.cache_resource
def load_retriever() -> RunbookRetriever:
    return RunbookRetriever()


@st.cache_resource
def load_incident_set() -> list[dict]:
    return load_incidents()


@st.cache_resource
def load_datasource(scenario: str):
    if config.DATA_SOURCE == "elasticsearch":
        return create_datasource("elasticsearch")
    return SyntheticDataSource.from_scenario(scenario)


@st.cache_resource(ttl=60)
def load_llm(provider: str):
    return get_llm(provider)


def session_key(scenario: str, provider: str) -> str:
    return f"{scenario}:{provider}"


def reset_session() -> None:
    st.session_state.thread_id = uuid.uuid4().hex
    st.session_state.checkpointer = InMemorySaver()
    st.session_state.result = None


# ---------- Kenar çubuğu ----------

with st.sidebar:
    st.header("⚙️ İnceleme ayarları")
    # URL parametreleri (?scenario=...&provider=...) tekrarlanabilir demo ve ekran görüntüsü içindir
    params = st.query_params
    scenarios = list(SCENARIOS)
    default_scenario = params.get("scenario", "error_spike")
    scenario = st.selectbox("Sentetik senaryo", scenarios, index=scenarios.index(default_scenario) if default_scenario in scenarios else 1,
                            format_func=lambda s: f"{s} · {SCENARIOS[s]}", disabled=config.DATA_SOURCE == "elasticsearch")
    providers = ["ollama", "gemini", "none"]
    default_provider = params.get("provider", config.LLM_PROVIDER)
    provider = st.selectbox("LLM sağlayıcı", providers, index=providers.index(default_provider) if default_provider in providers else 0)
    window = st.number_input("İncelenecek dönem (dk)", min_value=5, max_value=180, value=config.DEFAULT_WINDOW_MIN, step=5)
    baseline = st.number_input("Baseline dönemi (dk)", min_value=10, max_value=600, value=config.DEFAULT_BASELINE_MIN, step=10)
    service = st.selectbox("Servis filtresi", ["tümü", *config.SERVICES])
    fault_keys = list(FAULT_OPTIONS)
    fault_index = {"timeout": 1, "runbook_error": 2, "compare_error": 3}.get(params.get("fault", ""), 0)
    fault_label = st.selectbox("Araç hatası simülasyonu", fault_keys, index=fault_index)
    if st.button("🧹 Yeni oturum (hafızayı temizle)"):
        reset_session()
    st.caption(f"Veri kaynağı: `{config.DATA_SOURCE}` · Araç çağrısı sınırı: {config.MAX_TOOL_CALLS} · Tekrar deneme sınırı: {config.MAX_RETRIES}")

key = session_key(scenario, provider)
if st.session_state.get("session_key") != key:
    st.session_state.session_key = key
    reset_session()

try:
    datasource = load_datasource(scenario)
except (FileNotFoundError, RuntimeError) as exc:
    st.error(f"Veri kaynağı açılamadı: {exc}")
    st.stop()

llm, llm_status = load_llm(provider)

# ---------- Başlık ----------

st.title("🛡️ Payment Sentinel")
st.caption("Ödeme loglarındaki paternleri analiz eden, sapmaları ölçen ve kanıta dayalı rapor üreten inceleme agent'ı · sentetik veri")
if llm_status.available:
    st.success(f"Agent modu: **LLM** ({llm_status.provider}/{llm_status.model}). Talep yorumlama, plan ve araç seçimi LLM tarafından yapılır.", icon="🤖")
else:
    st.warning(f"**Sınırlı deterministik demo modu.** {llm_status.message} Araçlar sabit bir planla çağrılır.", icon="⚠️")

bounds = datasource.time_bounds()
if bounds:
    total = len(datasource.df) if hasattr(datasource, "df") else "?"
    st.caption(f"Veri aralığı: {bounds[0]:%Y-%m-%d %H:%M} – {bounds[1]:%H:%M} UTC · {total:,} kayıt · 'Şimdi' = son kayıt zamanı")

if "request_text" not in st.session_state:
    st.session_state.request_text = EXAMPLE_REQUESTS[0]
example = st.selectbox("Örnek talepler", ["—", *EXAMPLE_REQUESTS])
if example != "—" and st.session_state.get("last_example") != example:
    st.session_state.request_text = example
st.session_state.last_example = example
request = st.text_area("İnceleme talebi", key="request_text", height=80)

if st.button("🔍 Investigate Incident", type="primary"):
    toolkit = PaymentToolkit(datasource, load_retriever(), load_incident_set(), faults=FAULT_OPTIONS[fault_label])
    agent = InvestigationAgent(toolkit, llm if llm_status.available else None, checkpointer=st.session_state.checkpointer)
    ui_scope = {"window_minutes": int(window), "baseline_minutes": int(baseline), "service": None if service == "tümü" else service}
    with st.status("Agent inceleme yapıyor…", expanded=True) as status:
        seen = 0
        try:
            for node, trace in agent.stream(request, ui_scope, st.session_state.thread_id):
                for event in trace[seen:]:
                    label = event.get("tool") or event.get("detail", "")
                    extra = f" → {event.get('summary') or event.get('error', '')}" if event.get("tool") else ""
                    status.write(f"`{node}` · {label}{extra}")
                seen = max(seen, len(trace))
            st.session_state.result = agent.final_state(st.session_state.thread_id)
            status.update(label="İnceleme tamamlandı", state="complete", expanded=False)
        except Exception as exc:  # beklenmeyen hata: kullanıcıya anlaşılır mesaj
            status.update(label="İnceleme başarısız", state="error")
            st.error(f"İnceleme tamamlanamadı: {type(exc).__name__}: {exc}")

result = st.session_state.get("result")
if not result:
    st.info("Bir senaryo seçip **Investigate Incident** butonuna basın.")
    st.stop()

if not result.get("report"):
    st.warning(result.get("message") or "Rapor üretilemedi.")
    st.stop()

report = result["report"]
scope = report["scope"]
cmp = report["comparison"]
risk = report["risk"]

# ---------- KPI ve risk ----------

st.subheader(report["title"])
st.caption(f"Mod: {report['mode']} · Baseline: {scope['baseline_start'][11:16]}–{scope['baseline_end'][11:16]} UTC · Servis: {scope.get('service') or 'tümü'}")
if cmp:
    cur = cmp["current"]
    k = st.columns(5)
    k[0].metric("Toplam işlem", f"{cur['total_attempts']:,}", f"{cmp['volume_change_pct']:+.1f}% hacim/dk" if cmp["volume_change_pct"] is not None else None)
    k[1].metric("Başarısız işlem", f"{cur['failed']:,}", f"~{cmp['excess_failed']} fazladan", delta_color="inverse")
    k[2].metric("Hata oranı", f"%{cur['error_rate'] * 100:.2f}" if cur["error_rate"] is not None else "-",
                f"{cmp['error_rate_delta_pp']:+.2f} puan" if cmp["error_rate_delta_pp"] is not None else None, delta_color="inverse")
    k[3].metric("p95 gecikme", f"{cur['p95_latency_ms']} ms" if cur["p95_latency_ms"] is not None else "-",
                f"{cmp['p95_change_pct']:+.1f}%" if cmp["p95_change_pct"] is not None else None, delta_color="inverse")
    k[4].metric("Ortalama gecikme", f"{cur['avg_latency_ms']} ms" if cur["avg_latency_ms"] is not None else "-",
                f"{cmp['avg_latency_change_pct']:+.1f}%" if cmp["avg_latency_change_pct"] is not None else None, delta_color="inverse")

icon, kind = RISK_STYLE[risk["level"]]
score = f" · skor {risk['score']}/100" if risk["score"] is not None else ""
getattr(st, kind)(f"{icon} **Risk: {risk['level']}**{score} · **Veri güven düzeyi: {risk['confidence']}** (ayrı değerlendirilir) · _{risk['note']}_")
rc1, rc2 = st.columns([3, 2])
with rc1:
    if risk["reasons"]:
        st.dataframe(pd.DataFrame(risk["reasons"]).rename(columns={"factor": "Sinyal", "value": "Değer", "points": "Puan", "explanation": "Gerekçe"}),
                     hide_index=True, width="stretch")
    else:
        st.caption("Risk puanına katkı yapan sinyal yok.")
with rc2:
    st.markdown("**Güven gerekçeleri**")
    for reason in risk["confidence_reasons"]:
        st.markdown(f"- {reason}")

# ---------- Sekmeler ----------

tabs = st.tabs(["📄 Rapor", "📈 Zaman serisi", "🧩 Paternler", "⚠️ Hatalar ve servisler", "🕘 Benzer olaylar",
                "📚 Runbook kaynakları", "🔎 Agent izleri", "🧠 Hafıza"])

with tabs[0]:
    st.download_button("⬇️ Raporu indir (Markdown)", result["report_markdown"], file_name="incident_report.md", mime="text/markdown")
    st.markdown(result["report_markdown"])

with tabs[1]:
    # Grafik verisi doğrudan veri kaynağından deterministik olarak hesaplanır
    f = QueryFilter(parse_timestamp(scope["baseline_start"]), parse_timestamp(scope["current_end"]), scope.get("service"))
    hist = pd.DataFrame(datasource.histogram(f, config.BUCKET_MINUTES))
    if hist.empty:
        st.caption("Grafik için veri yok.")
    else:
        # Tarayıcı yerel saate çevirmesin diye UTC saat dilimsiz gösterilir
        hist["zaman"] = pd.to_datetime(hist["bucket_start"]).dt.tz_localize(None)
        hist["hata_orani_%"] = (hist["failed"] / hist["total"].where(hist["total"] > 0) * 100).round(2)
        hist = hist.set_index("zaman")
        st.caption(f"5 dakikalık dilimler. İncelenen dönem {scope['current_start'][11:16]} UTC'de başlıyor; öncesi baseline.")
        st.markdown("**Hata oranı (%)**")
        st.line_chart(hist[["hata_orani_%"]], height=220, color="#c2410c")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**p95 gecikme (ms)**")
            st.line_chart(hist[["p95_latency_ms"]], height=200, color="#1d4ed8")
        with c2:
            st.markdown("**İşlem hacmi (5 dk)**")
            st.bar_chart(hist[["total"]], height=200, color="#475569")
        if report["detected_patterns"]:
            st.caption(f"Anomali başlangıcı: {next((o['text'] for o in report['observations'] if 'başladı' in o['text']), '-')}")
        with st.expander("Tablo görünümü"):
            st.dataframe(hist[["total", "failed", "hata_orani_%", "avg_latency_ms", "p95_latency_ms"]], width="stretch")

with tabs[2]:
    rows = [
        {"Durum": "✅ tespit" if p["detected"] else ("⚪ veri yetersiz" if p["insufficient_data"] else "— yok"),
         "Patern": PATTERN_LABELS.get(p["type"], p["type"]), "Konu": p["subject"] or "", "Kanıt": p["evidence"],
         "Kural (demo eşik)": p["rule"], "n": p["sample_size"]}
        for p in report["detected_patterns"] + report["evaluated_patterns"]
    ]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("Anomali tespiti açıklanabilir istatistiksel kurallarla yapılır (oran farkı, iki oran z-testi, yüzde değişim). Eşikler demo amaçlıdır.")

with tabs[3]:
    st.markdown("**En sık hata kodları (incelenen dönem)**")
    codes = pd.DataFrame(report["affected_error_codes"])
    if codes.empty:
        st.caption("Başarısız işlem yok.")
    else:
        codes["share"] = (codes["share"] * 100).round(1)
        st.dataframe(codes.rename(columns={"key": "Hata kodu", "count": "Adet", "share": "Pay (%)", "baseline_count": "Baseline adet"}),
                     hide_index=True, width="stretch")
    analysis = next((r["output"] for r in reversed(result["results"]) if r["tool"] == "analyze_error_patterns" and r["status"] == "ok"), None)
    if analysis:
        st.markdown("**Servis karşılaştırması**")
        svc = pd.DataFrame(analysis["services"])
        for col in ("error_rate", "baseline_error_rate"):
            svc[col] = (svc[col] * 100).round(2)
        st.dataframe(svc.rename(columns={"service": "Servis", "total": "İşlem", "failed": "Başarısız", "error_rate": "Hata %",
                                         "baseline_error_rate": "Baseline hata %", "p95_latency_ms": "p95 ms",
                                         "baseline_p95_latency_ms": "Baseline p95 ms", "p95_change_pct": "p95 değişim %"}),
                     hide_index=True, width="stretch")

with tabs[4]:
    if report["similar_incidents"]:
        for m in report["similar_incidents"]:
            with st.container(border=True):
                status_label = "doğrulanmış neden" if m["cause_status"] == "verified" else "doğrulanmamış neden"
                st.markdown(f"**{m['incident_id']}** · benzerlik **{m['similarity']}** · {m['service']} · {', '.join(m['error_codes']) or 'hata kodu yok'}")
                st.markdown(f"- Neden ({status_label}): {m['cause']}\n- Çözüm: {m['resolution']}\n- Sonuç: {m['outcome']}")
                st.caption("Benzerlik dağılımı: " + ", ".join(f"{k}={v}" for k, v in m["breakdown"].items()))
    else:
        st.caption("Eşleşen geçmiş olay yok.")
    st.info(report["similar_incidents_note"])

with tabs[5]:
    runbooks = next((r["output"] for r in reversed(result["results"]) if r["tool"] == "search_runbooks" and r["status"] == "ok"), None)
    if not runbooks:
        st.caption("Runbook araması yapılmadı veya başarısız oldu.")
    else:
        st.caption(f"Sorgu: `{runbooks['query']}` · {runbooks['message']}")
        for hit in runbooks["hits"]:
            with st.expander(f"{hit['source']} › {hit['section'].split(' › ')[-1]} (skor {hit['score']})"):
                if hit["flagged"]:
                    st.warning("Bu parçada doküman içi talimat tespit edildi ve çıkarıldı (içerik güvenilmeyen veri olarak ele alınır).")
                st.text(hit["text"])

with tabs[6]:
    plan = next((t for t in result["trace"] if t["kind"] == "plan"), None)
    if plan:
        st.markdown(f"**{plan['detail']}**")
        st.markdown("\n".join(f"{i}. {s}" for i, s in enumerate(plan["steps"], 1)))
    rows = []
    for t in result["trace"]:
        if t["kind"] == "plan":
            continue
        rows.append({
            "Tür": t["kind"], "Düğüm": t["node"], "Araç": t.get("tool", ""),
            "Durum": t.get("status", "✅" if t.get("passed") else ("⚠️" if t.get("passed") is False else "")),
            "Sonuç / açıklama": t.get("summary") or t.get("error") or t.get("detail", ""),
            "Deneme": str(t.get("attempts", "")), "Süre (ms)": str(t.get("duration_ms", "")),
            "Argümanlar": json.dumps(t["args"], ensure_ascii=False) if t.get("args") else "",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=min(40 + 35 * len(rows), 760),
                 column_config={"Araç": st.column_config.TextColumn(width="medium"),
                                "Sonuç / açıklama": st.column_config.TextColumn(width="large"),
                                "Argümanlar": st.column_config.TextColumn(width="medium")})
    st.caption("Gözlemlenebilir işlem izleri gösterilir; modelin iç muhakemesi gösterilmez.")

with tabs[7]:
    st.caption(f"Oturum: `{st.session_state.thread_id[:8]}` · LangGraph checkpointer. Takip soruları önceki kapsamı kullanır.")
    for h in reversed(result.get("history", [])):
        st.markdown(f"- **{h['at'][11:19]}** · _{h['request'][:90]}_ → risk **{h['risk']}**, servis: {h['scope'].get('service') or 'tümü'}, "
                    f"paternler: {', '.join(h['detected']) or 'yok'}")
