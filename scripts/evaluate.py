"""
Etiketli sentetik senaryolar üzerinde tekrarlanabilir değerlendirme (deterministik agent, LLM yok).

Yöntem:
- Her senaryo × seed için agent, senaryonun etiketli talebiyle uçtan uca çalıştırılır.
- Tespitler `SCENARIO_LABELS` içindeki beklenen tespitlerle karşılaştırılır. Beklenen kapsam
  (ör. bank_id=BANK_B) tespitin kapsamında yoksa eşleşme sayılmaz. "acceptable" listesindeki gerçek
  ama zorunlu olmayan etkiler yanlış pozitif sayılmaz.
- Normal senaryolarda kayan pencerelerde yanlış pozitif oranı ayrıca ölçülür.
- Hata enjeksiyonlu çalıştırmalar inceleme tamamlama oranı için eklenir.

Sentetik değerlendirme üretim performansını göstermez.

Çalıştırma: python -m scripts.evaluate [--seeds 42 7 123]
"""

import argparse
import platform
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.agent import InvestigationAgent
from src.config import ROOT_DIR
from src.data_generator import SCENARIO_LABELS, SCENARIOS
from src.multiagent import Orchestrator
from src.repositories import SyntheticPaymentRepository
from src.retriever import RunbookRetriever
from src.tools import PaymentToolkit
from src.validation import parse_timestamp, to_iso

NORMAL = ["normal_baseline", "normal_fluctuation"]
FAULTS = {"breakdown_by_dimension": "error", "search_runbooks": "error", "analyze_latency_timeouts": "timeout_once"}
SALIENT = ("bank_id", "merchant_id", "service_name", "service_version", "response_code")


def key_of(d: dict) -> tuple:
    """Tespitin değerlendirme anahtarı: dedektör + belirleyici kapsam (izole segment, kod veya sürüm)."""
    if d["detector"] == "SEGMENT_ISOLATION":
        dim = d["extra"].get("dimension")
        return d["detector"], ((dim, d["scope"].get(dim)),)
    if d["detector"] == "RESPONSE_CODE_CONCENTRATION":
        return d["detector"], (("response_code", d["subject"]),)
    if d["detector"] == "SERVICE_VERSION_DEGRADATION":
        return d["detector"], (("service_version", d["subject"]),)
    return d["detector"], ()


def label_key(item: dict) -> tuple:
    return item["detector"], tuple(sorted(item.get("scope", {}).items()))


def evaluate_run(scenario: str, seed: int, retriever: RunbookRetriever, faults: dict | None = None,
                 multi_agent: bool = False) -> dict:
    repo = SyntheticPaymentRepository.from_scenario(scenario, seed)
    label = SCENARIO_LABELS[scenario]
    toolkit = PaymentToolkit(repo, retriever, faults=faults)
    started = time.perf_counter()
    if multi_agent:
        state = Orchestrator(toolkit).run(label["request"])
    else:
        state = InvestigationAgent(toolkit).run(label["request"], thread_id=f"{scenario}-{seed}")
    elapsed = time.perf_counter() - started
    report = state["report"]
    found = {key_of(d) for d in report["detections"]}
    expected = {label_key(x) for x in label["expected"]}
    acceptable = {label_key(x) for x in label.get("acceptable", [])}
    tp = found & expected
    return {
        "scenario": scenario, "seed": seed, "fault": ", ".join(f"{k}={v}" for k, v in (faults or {}).items()) or "-",
        "found": found, "tp": tp,
        "fp": found - expected - acceptable, "fn": expected - found,
        "severity": report["risk"]["severity"], "severity_ok": report["risk"]["severity"] in label["severities"],
        "status": report["status"], "insufficient_expected": label["insufficient"],
        "quality": report["risk"]["evidence_quality"], "quality_expected": label.get("evidence_quality"),
        "insufficient_flagged": report["status"] == "insufficient_evidence",
        "completed": report is not None and report["status"] in ("complete", "insufficient_evidence"),
        "report_produced": report is not None,
        "tool_calls": state["tool_calls"], "path": tuple(r["tool"] for r in state["results"]), "elapsed_s": elapsed,
        "validation_ok": all(v["passed"] for v in report["validation"] if v["check"].startswith("Rapor") or v["check"].startswith("Risk agent")),
        "agents": tuple((a["agent_name"], a["round"]) for a in state.get("agent_results", [])),
        "parallel": any(e["kind"] == "parallel" for e in state.get("trace", [])),
        "rounds": state.get("rounds", 1),
        "failed_agents": sum(a["status"] == "failed" for a in state.get("agent_results", [])),
    }


def multi_agent_section(seeds: list[int], retriever: RunbookRetriever) -> str:
    runs = [evaluate_run(s, seed, retriever, multi_agent=True) for seed in seeds for s in SCENARIOS]
    fault_runs = [evaluate_run(s, seeds[0], retriever, faults={tool: mode}, multi_agent=True)
                  for s in ("bank_timeout", "reversal_anomaly") for tool, mode in FAULTS.items()]
    tp, fp, fn = (sum(len(r[k]) for r in runs) for k in ("tp", "fp", "fn"))
    normal = [r for r in runs if r["scenario"] in NORMAL]
    times = [r["elapsed_s"] * 1000 for r in runs + fault_runs]
    summary = {
        "Çalıştırma (senaryo × seed)": len(runs),
        "Tespit precision / recall": f"{tp / (tp + fp):.3f} / {tp / (tp + fn):.3f}" if tp + fp and tp + fn else "-",
        "Doğru risk seviyesi": pct(sum(r["severity_ok"] for r in runs), len(runs)),
        "Normal senaryolarda tespit içeren inceleme": pct(sum(bool(r["found"]) for r in normal), len(normal)),
        "Yetersiz kanıt doğru işaretlendi": pct(sum(r["insufficient_flagged"] for r in runs if r["insufficient_expected"]),
                                                sum(r["insufficient_expected"] for r in runs)),
        "Paralel agent dalgası içeren inceleme": pct(sum(r["parallel"] for r in runs), len(runs)),
        "Takip turu açılan inceleme": pct(sum(r["rounds"] > 1 for r in runs), len(runs)),
        "Rapor + risk agent'ı tutarlılık kontrolü": pct(sum(r["validation_ok"] for r in runs + fault_runs), len(runs + fault_runs)),
        "Hata enjeksiyonunda rapor üretildi / eksik işaretlendi": f"{sum(r['report_produced'] for r in fault_runs)}/{len(fault_runs)} / "
                                                                  f"{sum(r['status'] == 'incomplete' for r in fault_runs)}",
        "Ort. araç çağrısı": f"{statistics.mean(r['tool_calls'] for r in runs):.1f}",
        "Süre medyan / p95 (ms)": f"{statistics.median(times):.0f} / {sorted(times)[int(0.95 * (len(times) - 1))]:.0f}",
    }
    rows = [{"Senaryo": s, "Seviye (seed'ler)": ", ".join(r["severity"] for r in runs if r["scenario"] == s),
             "Agent'lar (seed 1, tur)": ", ".join(f"{a}@{n}" for a, n in next(r for r in runs if r["scenario"] == s)["agents"]),
             "Paralel": "evet" if any(r["parallel"] for r in runs if r["scenario"] == s) else "hayır",
             "Takip turu": "evet" if any(r["rounds"] > 1 for r in runs if r["scenario"] == s) else "hayır"} for s in SCENARIOS]
    return (f"## Çoklu agent mimarisi\n\n{table([{'Ölçüt': k, 'Sonuç': v} for k, v in summary.items()])}\n\n{table(rows)}\n\n"
            + table([{"Senaryo": r["scenario"], "Hata": r["fault"], "Durum": r["status"], "Seviye": r["severity"],
                      "Başarısız agent": r["failed_agents"]} for r in fault_runs]) + "\n")


def rolling_false_positives(scenario: str, seed: int, retriever: RunbookRetriever) -> tuple[int, int]:
    tk = PaymentToolkit(SyntheticPaymentRepository.from_scenario(scenario, seed), retriever)
    start, end = parse_timestamp("2026-10-27T08:30:00Z"), parse_timestamp("2026-10-27T12:00:00Z")
    windows = alarms = 0
    while start + timedelta(minutes=60) <= end:
        cw = {"start": to_iso(start), "end": to_iso(start + timedelta(minutes=60))}
        bw = {"start": to_iso(start - timedelta(minutes=120)), "end": cw["start"]}
        out = tk.compare_with_baseline(cw, bw, {})
        windows += 1
        alarms += any(d["detected"] for d in out["detections"])
        start += timedelta(minutes=15)
    return alarms, windows


def pct(a: int, b: int) -> str:
    return f"{a}/{b} (%{100 * a / b:.1f})" if b else "-"


def table(rows: list[dict]) -> str:
    headers = list(rows[0])
    return "\n".join(["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
                     + ["| " + " | ".join(str(r[h]) for h in headers) + " |" for r in rows])


def run(seeds: list[int]) -> str:
    retriever = RunbookRetriever()
    runs = [evaluate_run(s, seed, retriever) for seed in seeds for s in SCENARIOS]
    fault_runs = [evaluate_run(s, seed, retriever, faults={tool: mode}) for seed in seeds[:1]
                  for s in ("bank_timeout", "version_regression", "reversal_anomaly") for tool, mode in FAULTS.items()]

    per_detector: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    for r in runs:
        for name, field in (("tp", "tp"), ("fp", "fp"), ("fn", "fn")):
            for det, _ in r[field]:
                per_detector[det][name] += 1
    det_rows = []
    for det, c in sorted(per_detector.items()):
        prec = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else None
        rec = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else None
        det_rows.append({"Dedektör": det, "TP": c["tp"], "FP": c["fp"], "FN": c["fn"],
                         "Precision": "-" if prec is None else f"{prec:.2f}", "Recall": "-" if rec is None else f"{rec:.2f}"})
    tp, fp, fn = (sum(len(r[k]) for r in runs) for k in ("tp", "fp", "fn"))

    normal_runs = [r for r in runs if r["scenario"] in NORMAL]
    rolling = [rolling_false_positives(s, seed, retriever) for seed in seeds for s in NORMAL]
    insuff = [r for r in runs if r["insufficient_expected"]]
    others = [r for r in runs if not r["insufficient_expected"]]
    scen_rows = []
    for s in SCENARIOS:
        rs = [r for r in runs if r["scenario"] == s]
        scen_rows.append({
            "Senaryo": s, "Seviye (seed'ler)": ", ".join(r["severity"] for r in rs),
            "Beklenen seviye": "/".join(SCENARIO_LABELS[s]["severities"]),
            "Recall": pct(sum(len(r["tp"]) for r in rs), sum(len(r["tp"]) + len(r["fn"]) for r in rs)),
            "FP": sum(len(r["fp"]) for r in rs), "Ort. araç çağrısı": round(statistics.mean(r["tool_calls"] for r in rs), 1),
            "Yol (seed 1)": " → ".join(t.replace("_", " ") for t in rs[0]["path"]),
        })
    fps = sorted({(r["scenario"], d, s) for r in runs for d, s in r["fp"]})
    all_runs = runs + fault_runs
    times = [r["elapsed_s"] * 1000 for r in all_runs]
    summary = {
        "Çalıştırma (senaryo × seed)": len(runs),
        "Toplam precision (tespit)": f"{tp / (tp + fp):.3f}" if tp + fp else "-",
        "Toplam recall (tespit)": f"{tp / (tp + fn):.3f}" if tp + fn else "-",
        "Doğru risk seviyesi": pct(sum(r["severity_ok"] for r in runs), len(runs)),
        "Normal senaryolarda tespit içeren inceleme (FP oranı)": pct(sum(bool(r["found"]) for r in normal_runs), len(normal_runs)),
        "Normal senaryolarda HIGH seviye": pct(sum(r["severity"] == "HIGH" for r in normal_runs), len(normal_runs)),
        "Kayan 60 dk pencerelerde alarm (normal)": pct(sum(a for a, _ in rolling), sum(w for _, w in rolling)),
        "Yetersiz kanıt doğru işaretlendi (low_volume)": pct(sum(r["insufficient_flagged"] and r["severity"] == "INDETERMINATE" for r in insuff), len(insuff)),
        "Yetersiz kanıt yanlış işaretlendi (diğer senaryolar)": pct(sum(r["insufficient_flagged"] for r in others), len(others)),
        "Beklenen kanıt kalitesi (data_quality_gap: limited)": pct(sum(r["quality"] == r["quality_expected"] for r in runs if r["quality_expected"]), sum(1 for r in runs if r["quality_expected"])),
        "İnceleme tamamlama (hata enjeksiyonsuz)": pct(sum(r["completed"] for r in runs), len(runs)),
        "Rapor üretildi (hata enjeksiyonlu)": pct(sum(r["report_produced"] for r in fault_runs), len(fault_runs)),
        "Tamamlandı / eksik olarak işaretlendi (hata enjeksiyonlu)": f"{sum(r['status'] == 'complete' for r in fault_runs)} / {sum(r['status'] == 'incomplete' for r in fault_runs)}",
        "Rapor-araç tutarlılık kontrolü geçti": pct(sum(r["validation_ok"] for r in all_runs), len(all_runs)),
        f"Farklı araç yolu sayısı (seed 1, {len(SCENARIOS)} senaryo)": len({r["path"] for r in runs if r["seed"] == seeds[0]}),
        "Ort. / maks. araç çağrısı": f"{statistics.mean(r['tool_calls'] for r in all_runs):.1f} / {max(r['tool_calls'] for r in all_runs)}",
        "Süre medyan / p95 (ms, inceleme başına)": f"{statistics.median(times):.0f} / {sorted(times)[int(0.95 * (len(times) - 1))]:.0f}",
    }
    env = f"Python {platform.python_version()} · {platform.system()} {platform.release()} · {platform.processor() or platform.machine()}"
    return (
        "# Değerlendirme sonuçları (sentetik)\n\n"
        f"Ölçüm zamanı: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · Ortam: {env} · Seed'ler: {', '.join(map(str, seeds))} · "
        "Mod: deterministik (LLM yok); önce tek agent (LangGraph), ardından çoklu agent mimarisi\n\n"
        "> Bu sonuçlar `python -m scripts.evaluate` çalıştırmasından otomatik üretilmiştir. Veriler ve etiketler sentetiktir; "
        "eşikler bu senaryolar için seçilmiştir. Sonuçlar üretim performansını göstermez.\n\n"
        f"## Tek agent (LangGraph) · özet\n\n{table([{'Ölçüt': k, 'Sonuç': v} for k, v in summary.items()])}\n\n"
        f"## Dedektör bazında\n\n{table(det_rows)}\n\n"
        f"## Senaryo bazında\n\n{table(scen_rows)}\n\n"
        "## Etiket dışı tespitler (yanlış pozitif sayılanlar)\n\n"
        + ("\n".join(f"- {s}: {d} {dict(sc)}" for s, d, sc in fps) or "- Yok.") + "\n\n"
        "## Hata enjeksiyonlu çalıştırmalar\n\n"
        + table([{"Senaryo": r["scenario"], "Hata": r["fault"], "Durum": r["status"], "Seviye": r["severity"],
                  "Araç çağrısı": r["tool_calls"]} for r in fault_runs]) + "\n\n"
        + multi_agent_section(seeds, retriever)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, nargs="+", default=[42, 7, 123])
    args = parser.parse_args()
    output = run(args.seeds)
    path = ROOT_DIR / "benchmarks" / "eval_results.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(output, encoding="utf-8")
    print(output)
    print(f"Kaydedildi: {path}")
