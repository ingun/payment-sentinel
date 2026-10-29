"""
Evidence Synthesizer: uzman çıktılarını tek, izlenebilir bir rapora birleştirir.

- Her bulgu ve hipotez için kanıt referanslarının EvidenceStore'da başarılı bir araç sonucuna karşılık
  geldiğini doğrular; desteksiz olanları reddeder. Birden fazla agent'ın aynı iddiayı tekrarlaması kanıt
  sayılmaz: yalnızca araç referansları sayılır.
- Kesin neden iddialarını reddeder; gerçek, hipotez ve doğrulanmış neden ayrımını korur.
- Bulguları tekilleştirir ve çelişkileri (izolasyon, tespit, kanıt kalitesi, geçmiş olay kapsamı) bulur.
  Çözülebilir çelişkiler için hedefli takip görevi önerir.
- Nihai raporu mevcut deterministik rapor üreticisiyle (src/report.py) kurar ve risk agent'ının hesabının
  raporla aynı olduğunu kontrol eder.
"""

from collections import defaultdict

from src.multiagent.contracts import AgentResult, Conflict, FollowUp
from src.multiagent.runtime import EvidenceStore
from src.policy import required_evidence
from src.report import CAUSAL_CLAIMS, build_report
from src.schemas import InvestigationReport
from src.tools import detections_of


def _norm_scope(scope: dict) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in scope.items()))


def verify_claims(results: list[AgentResult], store: EvidenceStore) -> tuple[list[dict], list[dict], list[dict]]:
    """Desteklenen bulgular (tekilleştirilmiş), desteklenen hipotezler ve reddedilen iddialar."""
    merged: dict[tuple, dict] = {}
    hypotheses: dict[str, dict] = {}
    rejected: list[dict] = []
    for res in results:
        for f in res.findings:
            valid = [r for r in f.evidence_refs if store.is_valid(r)]
            if not valid:
                rejected.append({"agent": res.agent_name, "claim": f.claim, "reason": "araç sonucuna dayanmıyor"})
                continue
            if CAUSAL_CLAIMS.search(f.claim):
                rejected.append({"agent": res.agent_name, "claim": f.claim, "reason": "kanıtlanmamış kesin neden iddiası"})
                continue
            key = (f.detector, _norm_scope(f.scope)) if f.detector else ("text", f.claim)
            item = merged.setdefault(key, {"claim": f.claim, "claim_type": f.claim_type, "detector": f.detector,
                                           "scope": f.scope, "evidence_refs": [], "agents": []})
            item["evidence_refs"] = sorted(set(item["evidence_refs"]) | set(valid), key=lambda r: int(r[1:]))
            if res.agent_name not in item["agents"]:
                item["agents"].append(res.agent_name)
        for h in res.hypotheses:
            valid = [r for r in h.evidence_refs if store.is_valid(r)]
            if not valid:
                rejected.append({"agent": res.agent_name, "claim": h.text, "reason": "hipotez araç sonucuna dayanmıyor"})
            elif CAUSAL_CLAIMS.search(h.text):
                rejected.append({"agent": res.agent_name, "claim": h.text, "reason": "kanıtlanmamış kesin neden iddiası"})
            else:
                entry = hypotheses.setdefault(h.text, {"text": h.text, "evidence_refs": [], "agents": [], "status": "doğrulanmadı"})
                entry["evidence_refs"] = sorted(set(entry["evidence_refs"]) | set(valid), key=lambda r: int(r[1:]))
                if res.agent_name not in entry["agents"]:
                    entry["agents"].append(res.agent_name)
    return list(merged.values()), list(hypotheses.values()), rejected


def find_conflicts(results: list[AgentResult], store: EvidenceStore) -> list[Conflict]:
    conflicts: list[Conflict] = []

    # 1. İzolasyon: aynı boyut ve metrik için farklı segment iddiaları
    by_key: dict[tuple, list] = defaultdict(list)
    for res in results:
        for a in res.assertions:
            if all(store.is_valid(r) for r in a.evidence_refs):
                by_key[(a.dimension, a.metric)].append((res.agent_name, a))
    for (dimension, metric), items in by_key.items():
        segments = {a.isolated_segment for _, a in items}
        if len(segments) > 1:
            conflicts.append(Conflict(
                kind="isolation", agents=sorted({n for n, _ in items}),
                evidence_refs=sorted({r for _, a in items for r in a.evidence_refs}),
                description=f"{metric} metriği için {dimension} izolasyonunda farklı sonuçlar: "
                            + ", ".join(f"{n}={a.isolated_segment or 'izole değil'}" for n, a in items),
                follow_up=FollowUp(agent="anomaly_detection", reason=f"{dimension}/{metric} izolasyonu yeniden doğrulanmalı.",
                                   questions=[f"verify_isolation:{dimension}:{metric}"]),
            ))

    # 2. Tespit: aynı pencere ve kapsamda aynı dedektör farklı araç sonuçlarında farklı karar vermiş
    verdicts: dict[tuple, set] = defaultdict(set)
    refs: dict[tuple, set] = defaultdict(set)
    for r in store.results:
        if r.get("status") != "ok":
            continue
        for d in detections_of(r["tool"], r["output"]):
            if d["evidence_quality"] == "insufficient":
                continue
            key = (d["detector"], d["subject"] if d["detector"] != "SEGMENT_ISOLATION" else None,
                   _norm_scope(d["scope"]), str(d["window"]), str(d["baseline_window"]))
            verdicts[key].add(d["detected"])
            refs[key].add(r["ref"])
    for key, values in verdicts.items():
        if len(values) > 1:
            conflicts.append(Conflict(kind="detection", agents=sorted({store.get(x)["agent"] for x in refs[key]}),
                                      evidence_refs=sorted(refs[key]),
                                      description=f"{key[0]} aynı kapsamda ({dict(key[2]) or 'tümü'}) farklı araç sonuçlarında farklı karar verdi."))

    # 3. Kanıt kalitesi: anomali agent'ı veri yetersiz derken diğer agent'lar bulgu raporluyor
    insufficient = [r for r in results if r.agent_name == "anomaly_detection" and r.status == "insufficient_data"]
    others = [r for r in results if r.agent_name not in ("anomaly_detection", "impact_risk") and r.findings]
    if insufficient and others:
        conflicts.append(Conflict(kind="evidence_quality", agents=["anomaly_detection"] + [r.agent_name for r in others],
                                  description="Anomali agent'ı örneklemi yetersiz bulurken diğer agent'lar bulgu raporladı; "
                                              "bu bulgular zayıf kanıt olarak değerlendirilmelidir."))

    # 4. Geçmiş olay kapsamı: en benzer olay farklı bir segmenti etkilemiş
    isolated = {a.dimension: a.isolated_segment for r in results for a in r.assertions if a.isolated_segment}
    for r in store.results:
        if r["tool"] == "search_similar_incidents" and r.get("status") == "ok" and r["output"]["matches"]:
            top = r["output"]["matches"][0]
            for dim, seg in isolated.items():
                if dim in top["scope"] and top["scope"][dim] != seg:
                    conflicts.append(Conflict(kind="incident_scope", agents=["historical_incident", "anomaly_detection"],
                                              evidence_refs=[r["ref"]],
                                              description=f"En benzer olay {top['incident_id']} {dim}={top['scope'][dim]} kapsamındaydı; "
                                                          f"bu incelemede {dim}={seg}. Belirti benzer, kapsam farklı."))
    return conflicts


def synthesize(*, question: str, scope: dict, assumptions: list[str], store: EvidenceStore, results: list[AgentResult],
               skipped: list[dict], conflicts: list[Conflict], rounds: int, data_source: str, mode: str,
               llm_findings: list[str] | None = None, budget_note: str | None = None) -> tuple[InvestigationReport, dict]:
    findings, hypotheses, rejected = verify_claims(results, store)
    failed = [r for r in results if r.status == "failed"]
    missing = required_evidence(scope, store.results)
    limitations = []
    for r in failed:
        limitations.append(f"Eksik analiz: {r.agent_name} agent'ı başarısız oldu ({'; '.join(r.errors) or 'bilinmeyen hata'}). "
                           "Diğer agent'ların bulguları geçerliliğini korur.")
    for r in results:
        if r.status == "partial":
            limitations.append(f"{r.agent_name} kısmen tamamlandı: {'; '.join(r.errors)}")
        limitations += [f"{r.agent_name}: {x}" for x in r.limitations]
    for c in conflicts:
        if not c.resolved:
            limitations.append(f"Çözülmemiş çelişki ({c.kind}): {c.description}")
    if budget_note:
        limitations.append(budget_note)
    for s in skipped:
        limitations.append(f"{s['agent']} çalıştırılmadı: {s['reason']}")

    # Agent bulguları rapora, kanıt referansları ve agent adlarıyla girer; sayılar yine doğrulanır
    agent_lines = [f"[{', '.join(f['agents'])}] {f['claim']} ({', '.join(f['evidence_refs'])})" for f in findings]
    report = build_report(
        question=question, scope=scope, results=store.results, mode=mode, data_source=data_source,
        assumptions=assumptions, limitations=list(dict.fromkeys(limitations)),
        missing=missing + [f"agent:{r.agent_name}" for r in failed],
        validation_log=[{"check": "Zorunlu kanıtlar mevcut", "passed": not missing,
                         "details": [f"Eksik: {', '.join(missing)}"] if missing else []}],
        agent_findings=agent_lines + list(llm_findings or []),
    )
    # Başarısız veya kısmi (bir aracı hata vermiş) agent varsa analiz eksiktir; bu açıkça raporlanır
    if (failed or any(r.status == "partial" for r in results)) and report.status == "complete":
        report.status = "incomplete"

    # Risk agent'ının hesabı ile nihai raporun riski aynı olmalı (aynı girdiler, aynı deterministik fonksiyon)
    risk_runs = [r for r in store.results if r["tool"] == "assess_impact"]
    if risk_runs:
        agent_risk = risk_runs[-1]["output"]
        same = agent_risk["severity"] == report.risk.severity and agent_risk["score"] == report.risk.score
        report.validation.append({"check": "Risk agent'ı hesabı nihai raporla tutarlı", "passed": same,
                                  "details": [] if same else [f"agent: {agent_risk['severity']}/{agent_risk['score']}, rapor: "
                                                               f"{report.risk.severity}/{report.risk.score}"]})
    report.validation.append({"check": "Agent bulguları araç sonuçlarına dayanıyor", "passed": not rejected,
                              "details": [f"{x['agent']}: {x['claim'][:80]} ({x['reason']})" for x in rejected]})
    report.agent_runs = [{"agent": r.agent_name, "round": r.round, "status": r.status, "summary": r.summary,
                          "elapsed_ms": r.elapsed_ms, "tool_calls": r.tool_calls, "evidence_refs": r.evidence_references,
                          "errors": r.errors} for r in results]
    report.conflicts = [c.model_dump() for c in conflicts]
    report.orchestration = {"rounds": rounds, "skipped": skipped, "rejected_claims": rejected,
                            "merged_findings": findings, "agent_hypotheses": hypotheses}
    return report, {"findings": findings, "hypotheses": hypotheses, "rejected": rejected}
