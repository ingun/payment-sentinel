"""
Gerçek LLM ile uçtan uca agent değerlendirmesi.

Yöntem: Her senaryo, farklı ifade edilmiş taleplerle LLM modunda çalıştırılır. Beklenen risk seviyesi,
aynı talebin deterministik moddaki sonucudur (sayılar her iki modda da aynı araçlardan gelir).
Ölçülenler: doğru risk seviyesi, zorunlu kanıtların tamamlanması, LLM'in akışı sonuna kadar yönetmesi
(yedek moda düşmeden), bulgu özetinin doğrulamadan geçmesi, uydurulan kapsamın yakalanması ve süre.
Ayrıca kapsam dışı bir talebin her denemede reddedildiği kontrol edilir.

Çalıştırma: python -m scripts.llm_eval [--provider ollama] [--scenarios normal error_spike]
"""

import argparse
import platform
import statistics
import time
from datetime import datetime, timezone

from src.agent import InvestigationAgent
from src.config import ROOT_DIR
from src.datasource import SyntheticDataSource
from src.incident_matcher import load_incidents
from src.llm import get_llm
from src.retriever import RunbookRetriever
from src.tools import PaymentToolkit

SCENARIOS = ["normal", "error_spike", "latency_degradation", "volume_drop"]
REQUESTS = [
    "Son 30 dakikadaki ödeme başarısızlıklarını araştır, normal dönemle karşılaştır ve olası incident nedenlerini raporla.",
    "Son 30 dakikadaki ödeme hatalarını araştır ve normal dönemle karşılaştır.",
    "Ödemelerde bir sorun var gibi, kontrol eder misin?",
]
OFF_TOPIC = "Bana bir şiir yaz"


def run_once(scenario: str, request: str, llm, retriever, incidents) -> dict:
    toolkit = PaymentToolkit(SyntheticDataSource.from_scenario(scenario), retriever, incidents)
    expected = InvestigationAgent(toolkit).run(request, thread_id="expected")["report"]["risk"]["level"]
    t = time.perf_counter()
    state = InvestigationAgent(toolkit, llm).run(request, thread_id="eval")
    duration = time.perf_counter() - t
    report = state.get("report") or {}
    trace = state.get("trace", [])
    return {
        "scenario": scenario,
        "expected": expected,
        "actual": (report.get("risk") or {}).get("level", "REDDEDİLDİ"),
        "evidence_complete": bool(report) and not state.get("missing"),
        "llm_drove": state.get("mode") == "llm",
        "tool_calls": state.get("tool_calls", 0),
        "accepted": len(report.get("agent_findings", [])),
        "rejected": sum(1 for v in report.get("validation", []) if v["check"] == "Agent bulgusu reddedildi"),
        "scope_dropped": any(t["kind"] == "validation" and t["node"] == "parse_request" for t in trace),
        "llm_errors": sum(1 for t in trace if t["kind"] == "llm_error"),
        "duration_s": round(duration, 1),
    }


def pct(part: int, total: int) -> str:
    return f"{part}/{total} (%{100 * part / total:.0f})" if total else "-"


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    accepted, rejected = sum(r["accepted"] for r in rows), sum(r["rejected"] for r in rows)
    return {
        "Çalıştırma": n,
        "Doğru risk seviyesi": pct(sum(r["actual"] == r["expected"] for r in rows), n),
        "Zorunlu kanıt tamam": pct(sum(r["evidence_complete"] for r in rows), n),
        "LLM sona kadar yönetti": pct(sum(r["llm_drove"] for r in rows), n),
        "Bulgu özeti yazıldı": pct(sum(r["accepted"] + r["rejected"] > 0 for r in rows), n),
        "Kabul / reddedilen bulgu": f"{accepted} / {rejected}",
        "Uydurulan kapsam yakalandı": sum(r["scope_dropped"] for r in rows),
        "Ort. araç çağrısı": round(statistics.mean(r["tool_calls"] for r in rows), 1),
        "Medyan süre (sn)": statistics.median(r["duration_s"] for r in rows),
    }


def table(rows: list[dict]) -> str:
    headers = list(rows[0])
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(r[h]) for h in headers) + " |" for r in rows]
    return "\n".join(lines)


def run(provider: str, scenarios: list[str]) -> str:
    llm, status = get_llm(provider)
    if llm is None:
        raise SystemExit(f"LLM erişilemiyor: {status.message}")
    retriever, incidents = RunbookRetriever(), load_incidents()
    rows = []
    for scenario in scenarios:
        for request in REQUESTS:
            rows.append(run_once(scenario, request, llm, retriever, incidents))
            print(rows[-1], flush=True)

    off_topic = []
    for _ in range(len(REQUESTS)):
        state = InvestigationAgent(PaymentToolkit(SyntheticDataSource.from_scenario("normal"), retriever, incidents), llm).run(
            OFF_TOPIC, thread_id="off")
        off_topic.append(state.get("report") is None)

    per_scenario = [{"Senaryo": s, **summarize([r for r in rows if r["scenario"] == s])} for s in scenarios]
    overall = {"Senaryo": "**Toplam**", **summarize(rows)}
    details = [{"Senaryo": r["scenario"], "Talep": REQUESTS[i % len(REQUESTS)][:45] + "…", "Beklenen": r["expected"],
                "LLM": r["actual"], "Araç": r["tool_calls"], "Bulgu (kabul/red)": f"{r['accepted']}/{r['rejected']}",
                "Süre (sn)": r["duration_s"]} for i, r in enumerate(rows)]
    env = f"Python {platform.python_version()} · {platform.system()} {platform.release()} · {status.provider}/{status.model}"
    return (
        f"# LLM agent değerlendirmesi\n\nÖlçüm zamanı: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n\nOrtam: {env}\n\n"
        f"Yöntem: {len(scenarios)} senaryo × {len(REQUESTS)} farklı talep, gerçek LLM ile uçtan uca. Beklenen risk seviyesi, "
        "aynı talebin deterministik moddaki sonucudur. \"LLM sona kadar yönetti\": akış yedek deterministik moda düşmeden "
        "bitti. \"Bulgu özeti\": LLM'in yazdığı bulgular; reddedilenler araç çıktısında olmayan sayı veya kesin neden "
        "iddiası içerir ve rapora girmez.\n\n"
        f"## Özet\n\n{table(per_scenario + [overall])}\n\n"
        f"Kapsam dışı talep (\"{OFF_TOPIC}\") reddedildi: {pct(sum(off_topic), len(off_topic))}\n\n"
        f"## Çalıştırmalar\n\n{table(details)}\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="ollama")
    parser.add_argument("--scenarios", nargs="+", default=SCENARIOS)
    args = parser.parse_args()
    output = run(args.provider, args.scenarios)
    path = ROOT_DIR / "benchmarks" / "llm_eval.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(output, encoding="utf-8")
    print(output)
    print(f"Kaydedildi: {path}")
