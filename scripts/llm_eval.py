"""
Gerçek LLM ile uçtan uca değerlendirme (tek agent ve çoklu agent).

Yöntem:
- Her senaryo etiketli talebiyle, üç senaryo ayrıca iki farklı ifadeyle, iki mimaride de gerçek LLM ile çalıştırılır.
- Beklenen risk seviyesi, aynı talebin aynı mimarideki deterministik (LLM'siz) sonucudur. Etiketli taleplerde
  ayrıca senaryo etiketlerindeki beklenen tespitler ve seviye kontrol edilir.
- Ölçülenler: doğru seviye, etiketle uyum, zorunlu kanıt, LLM'in akışı yönetmesi (tek agent: yedek moda düşmeden;
  çoklu agent: LLM agent seçimi başarılı), bulgu özeti kabul/red, uydurulan kapsamın yakalanması, süre.
- Kapsam dışı talebin reddedildiği her iki mimaride kontrol edilir.

Çalıştırma: python -m scripts.llm_eval [--provider ollama] [--arch both|single|multi]
"""

import argparse
import platform
import statistics
import time
from datetime import datetime, timezone

from src.agent import InvestigationAgent
from src.config import ROOT_DIR
from src.data_generator import SCENARIO_LABELS, SCENARIOS
from src.llm import get_llm
from src.multiagent import Orchestrator
from src.repositories import JsonIncidentRepository, SyntheticPaymentRepository
from src.retriever import RunbookRetriever
from src.tools import PaymentToolkit

VARIANTS = ["Son 1 saatteki ödeme hatalarını araştır ve normal dönemle karşılaştır.",
            "Ödemelerde bir sorun var gibi, kontrol eder misin?"]
VARIANT_SCENARIOS = ["normal_baseline", "bank_timeout", "reversal_anomaly"]
OFF_TOPIC = "Bana bir şiir yaz"


def key_of(d: dict) -> tuple:
    if d["detector"] == "SEGMENT_ISOLATION":
        dim = d["extra"].get("dimension")
        return d["detector"], ((dim, d["scope"].get(dim)),)
    if d["detector"] in ("RESPONSE_CODE_CONCENTRATION", "SERVICE_VERSION_DEGRADATION"):
        field = "response_code" if d["detector"] == "RESPONSE_CODE_CONCENTRATION" else "service_version"
        return d["detector"], ((field, d["subject"]),)
    return d["detector"], ()


def investigate(arch: str, scenario: str, request: str, llm, retriever, incidents) -> dict:
    toolkit = PaymentToolkit(SyntheticPaymentRepository.from_scenario(scenario), retriever, incidents)
    if arch == "single":
        return InvestigationAgent(toolkit, llm).run(request, thread_id=f"{scenario}-{llm is not None}")
    return Orchestrator(toolkit, llm=llm).run(request)


def run_once(arch: str, scenario: str, request: str | None, llm, retriever, incidents) -> dict:
    labeled = request is None
    request = request or SCENARIO_LABELS[scenario]["request"]
    expected = investigate(arch, scenario, request, None, retriever, incidents)["report"]["risk"]["severity"]
    t = time.perf_counter()
    state = investigate(arch, scenario, request, llm, retriever, incidents)
    duration = time.perf_counter() - t
    report = state.get("report") or {}
    trace = state.get("trace", [])
    label_ok = None
    if labeled and report:
        label = SCENARIO_LABELS[scenario]
        found = {key_of(d) for d in report["detections"]}
        wanted = {(x["detector"], tuple(sorted(x.get("scope", {}).items()))) for x in label["expected"]}
        label_ok = wanted <= found and report["risk"]["severity"] in label["severities"]
    if arch == "single":
        llm_drove = state.get("mode") == "llm"
    else:
        llm_drove = any(e["kind"] == "decision" and "LLM agent seçimi" in e["detail"] for e in trace)
    return {
        "arch": arch, "scenario": scenario, "labeled": labeled, "expected": expected,
        "actual": (report.get("risk") or {}).get("severity", "REDDEDİLDİ"), "label_ok": label_ok,
        "evidence_complete": bool(report) and report.get("status") in ("complete", "insufficient_evidence"),
        "llm_drove": llm_drove, "tool_calls": state.get("tool_calls", 0),
        "accepted": sum(1 for f in report.get("agent_findings", []) if not f.startswith("[")),
        "rejected": sum(1 for v in report.get("validation", []) if v["check"] == "Agent bulgusu reddedildi"),
        "scope_dropped": any(t["kind"] == "validation" and t["node"] == "parse_request" for t in trace),
        "llm_errors": sum(1 for t in trace if t["kind"] == "llm_error"),
        "duration_s": round(duration, 1),
    }


def pct(part: int, total: int) -> str:
    return f"{part}/{total} (%{100 * part / total:.0f})" if total else "-"


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    labeled = [r for r in rows if r["labeled"]]
    return {
        "Çalıştırma": n,
        "Deterministik ile aynı seviye": pct(sum(r["actual"] == r["expected"] for r in rows), n),
        "Etiketle uyumlu (beklenen tespitler + seviye)": pct(sum(bool(r["label_ok"]) for r in labeled), len(labeled)),
        "Kanıt tamam (rapor tamamlandı / kanıt yetersiz doğru)": pct(sum(r["evidence_complete"] for r in rows), n),
        "LLM akışı yönetti": pct(sum(r["llm_drove"] for r in rows), n),
        "LLM bulgusu kabul / red": f"{sum(r['accepted'] for r in rows)} / {sum(r['rejected'] for r in rows)}",
        "Uydurulan kapsam yakalandı": sum(r["scope_dropped"] for r in rows),
        "LLM hata olayı": sum(r["llm_errors"] for r in rows),
        "Ort. araç çağrısı": round(statistics.mean(r["tool_calls"] for r in rows), 1),
        "Medyan / maks. süre (sn)": f"{statistics.median(r['duration_s'] for r in rows)} / {max(r['duration_s'] for r in rows)}",
    }


def table(rows: list[dict]) -> str:
    headers = list(rows[0])
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(r[h]) for h in headers) + " |" for r in rows]
    return "\n".join(lines)


def run(provider: str, archs: list[str]) -> str:
    llm, status = get_llm(provider)
    if llm is None:
        raise SystemExit(f"LLM erişilemiyor: {status.message}")
    retriever, incidents = RunbookRetriever(), JsonIncidentRepository()
    plan = [(s, None) for s in SCENARIOS] + [(s, v) for s in VARIANT_SCENARIOS for v in VARIANTS]
    rows, off_topic = [], {}
    for arch in archs:
        for scenario, request in plan:
            rows.append(run_once(arch, scenario, request, llm, retriever, incidents))
            print(rows[-1], flush=True)
        out = investigate(arch, "normal_baseline", OFF_TOPIC, llm, retriever, incidents)
        off_topic[arch] = out.get("report") is None

    names = {"single": "Tek agent (LangGraph)", "multi": "Çoklu agent"}
    overall = [{"Mimari": names[a], **summarize([r for r in rows if r["arch"] == a])} for a in archs]
    details = [{"Mimari": names[r["arch"]], "Senaryo": r["scenario"], "Talep": "etiketli" if r["labeled"] else "alternatif",
                "Deterministik": r["expected"], "LLM": r["actual"],
                "Etiket": {True: "✓", False: "✗", None: "-"}[r["label_ok"]], "LLM yönetti": "✓" if r["llm_drove"] else "✗",
                "Araç": r["tool_calls"], "Bulgu (kabul/red)": f"{r['accepted']}/{r['rejected']}", "Süre (sn)": r["duration_s"]}
               for r in rows]
    env = f"Python {platform.python_version()} · {platform.system()} {platform.release()} · {status.provider}/{status.model}"
    return (
        f"# Gerçek LLM ile değerlendirme\n\nÖlçüm zamanı: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · Ortam: {env}\n\n"
        f"Yöntem: {len(SCENARIOS)} senaryo etiketli talebiyle, {len(VARIANT_SCENARIOS)} senaryo ayrıca {len(VARIANTS)} farklı ifadeyle; "
        "her mimaride gerçek LLM ile uçtan uca. \"Deterministik ile aynı seviye\": aynı talebin aynı mimarideki LLM'siz sonucu. "
        "\"LLM akışı yönetti\": tek agent'ta akış yedek moda düşmeden bitti; çoklu agent'ta LLM agent seçimi geçerli yapılandırılmış "
        "çıktı üretti. LLM bulgularındaki sayılar araç çıktılarıyla doğrulanır; ifadelerin anlamı otomatik doğrulanmaz.\n\n"
        "> Sentetik veri ve tek bir yerel model (qwen3.5:4b) ile küçük bir örneklemdir; üretim performansını göstermez.\n\n"
        f"## Özet\n\n{table(overall)}\n\n"
        + "\n".join(f"Kapsam dışı talep (\"{OFF_TOPIC}\") reddedildi — {names[a]}: {'evet' if ok else 'hayır'}" for a, ok in off_topic.items())
        + f"\n\n## Çalıştırmalar\n\n{table(details)}\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="ollama")
    parser.add_argument("--arch", choices=["both", "single", "multi"], default="both")
    args = parser.parse_args()
    archs = ["single", "multi"] if args.arch == "both" else [args.arch]
    output = run(args.provider, archs)
    path = ROOT_DIR / "benchmarks" / "llm_eval.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(output, encoding="utf-8")
    print(output)
    print(f"Kaydedildi: {path}")
