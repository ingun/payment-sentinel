"""
Uzman agent'lar. Her agent'ın ayrı bir sorumluluğu, kendi araç izin listesi ve yapılandırılmış çıktısı vardır.

- AnomalyDetectionAgent: oranlar, yanıt kodları, hacim, gecikme; segment izolasyonu ve sürüm karşılaştırması.
- PaymentFlowAgent: POS/authorization/reversal/refund/timeout ilişkileri, correlation bağları, mutabakat.
- HistoricalIncidentAgent: geçmiş incident'lar ve runbook'lar (içerik güvenilmeyen veri).
- ImpactRiskAgent: deterministik risk hesabı; parasal kayıp üretmez.

Agent'ların kararları deterministiktir (LLM gerekmez); sayılar yalnızca araç çıktılarından gelir ve her bulgu
EvidenceStore referansı taşır. Agent'lar birbirinin serbest metnini değil, paylaşılan kanıt deposunu okur.
"""

import time

from src.multiagent.contracts import (
    AgentHypothesis,
    AgentResult,
    AgentTask,
    Assertion,
    Finding,
    FollowUp,
    InvestigationContext,
)
from src.multiagent.runtime import BudgetExceeded, EvidenceStore, ToolRunner
from src.policy import runbook_query, signals
from src.report import DETECTOR_LABELS
from src.risk_engine import assess_risk
from src.tools import detections_of

REVERSAL_FOCUS = ("merchant_id", "bank_id", "service_name")


class _Run:
    """Tek bir agent çalıştırmasının araç çağrılarını, referanslarını ve hatalarını toplar."""

    def __init__(self, agent: "SpecialistAgent", task: AgentTask, runner: ToolRunner):
        self.agent, self.task, self.runner = agent, task, runner
        self.refs: list[str] = []
        self.errors: list[str] = []
        self.calls = 0
        self.findings: list[Finding] = []
        self.hypotheses: list[AgentHypothesis] = []
        self.assertions: list[Assertion] = []
        self.limitations: list[str] = []
        self.follow_up: list[FollowUp] = []
        self.metrics: dict = {}

    async def call(self, tool: str, args: dict, reason: str) -> tuple[dict | None, str | None]:
        try:
            output, ref = await self.runner.call(self.agent.name, self.agent.tools, tool, args, reason, self.task.round)
        except BudgetExceeded as exc:
            self.errors.append(str(exc))
            return None, None
        self.calls += 1
        self.refs.append(ref)
        if output is None:
            entry = self.runner.store.get(ref)
            self.errors.append(f"{tool}: {entry.get('error', 'hata')}")
        return output, ref

    def add_detections(self, tool: str, output: dict, ref: str) -> list[dict]:
        found = [d for d in detections_of(tool, output) if d["detected"]]
        for d in found:
            label = DETECTOR_LABELS.get(d["detector"], d["detector"])
            self.findings.append(Finding(
                claim=f"{label}: {d['evidence']}", detector=d["detector"], evidence_refs=[ref],
                claim_type="correlation" if d["claim_type"] == "correlation" else "observation",
                scope={k: str(v) for k, v in d["scope"].items()},
            ))
        return found

    def result(self, status: str, summary: str, started: float) -> AgentResult:
        if self.errors and status == "success":
            status = "partial"
        return AgentResult(
            agent_name=self.agent.name, status=status, summary=summary, findings=self.findings,
            supporting_metrics=self.metrics, evidence_references=self.refs, hypotheses=self.hypotheses,
            assertions=self.assertions, limitations=self.limitations, recommended_follow_up=self.follow_up,
            errors=self.errors, round=self.task.round, tool_calls=self.calls,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )


class SpecialistAgent:
    name: str = ""
    tools: frozenset[str] = frozenset()
    description: str = ""

    async def run(self, task: AgentTask, ctx: InvestigationContext, runner: ToolRunner) -> AgentResult:
        started = time.perf_counter()
        run = _Run(self, task, runner)
        return await self._investigate(run, task, ctx, runner.store, started)

    async def _investigate(self, run: _Run, task: AgentTask, ctx: InvestigationContext, store: EvidenceStore,
                           started: float) -> AgentResult:
        raise NotImplementedError


def _pair(ctx: InvestigationContext, filters: dict, **extra) -> dict:
    cw, bw = ctx.windows()
    return {"current_window": cw, "baseline_window": bw, "filters": filters, **extra}


def _scope_text(filters: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in filters.items()) or "tüm trafik"


# ---------- Anomali tespiti ----------


class AnomalyDetectionAgent(SpecialistAgent):
    name = "anomaly_detection"
    description = "Hata/red/timeout oranları, yanıt kodları, hacim ve gecikmeyi baseline ile karşılaştırır; etkilenen boyutu bulur."
    tools = frozenset({"check_data_coverage", "compare_with_baseline", "breakdown_by_dimension",
                       "analyze_response_codes", "analyze_latency_timeouts", "compare_service_versions"})

    async def _investigate(self, run, task, ctx, store, started):
        base_filters = {**ctx.filters, **task.focus_filters}
        cw, _ = ctx.windows()
        where = _scope_text(base_filters)

        coverage, cov_ref = await run.call("check_data_coverage", {"window": cw, "filters": base_filters},
                                           f"Kapsam ({where}) için veri kapsamı ve örneklem yeterliliği kontrol ediliyor.")
        baseline, base_ref = await run.call("compare_with_baseline", _pair(ctx, base_filters),
                                            f"Kapsam ({where}) baseline ve dünün aynı penceresiyle karşılaştırılıyor.")
        if baseline is None:
            return run.result("failed", "Baseline karşılaştırması alınamadı; anomali değerlendirilemedi.", started)
        run.metrics.update({"current_transactions": baseline["current"]["total"], **{f"delta_{k}": v for k, v in baseline["deltas"].items()},
                            **{f"excess_{k}": v for k, v in baseline["excess"].items()}})
        if coverage and coverage["evidence_quality"] == "insufficient":
            run.limitations += coverage["issues"]
            run.findings.append(Finding(claim=f"Kanıt yetersiz: pencerede {coverage['primary_transactions']} birincil işlem var; "
                                              "anomali iddia edilmedi.", evidence_refs=[cov_ref]))
            return run.result("insufficient_data", "Örneklem yetersiz; güçlü bir anomali sonucu üretilmedi.", started)

        run.add_detections("compare_with_baseline", baseline, base_ref)
        sig = signals(baseline)
        focus = dict(base_filters)

        # Görev bir izolasyon doğrulaması istiyorsa (ör. çelişki çözümü) yalnızca onu yap
        checks = [q.split(":", 2) for q in task.questions if q.startswith("verify_isolation:")]
        if checks:
            for _, dimension, metric in checks:
                out, ref = await run.call("breakdown_by_dimension", _pair(ctx, base_filters, dimension=dimension, metric=metric),
                                          f"Çelişki çözümü: {metric} metriğinin {dimension} boyutunda izolasyonu yeniden test ediliyor.")
                if out:
                    self._record_isolation(run, out, ref)
            return run.result("success", "Hedefli izolasyon doğrulaması tamamlandı.", started)

        if sig["technical"] or sig["timeout"]:
            for dim in ("bank_id", "service_name"):
                if dim in base_filters:
                    continue
                out, ref = await run.call("breakdown_by_dimension", _pair(ctx, base_filters, dimension=dim, metric="technical_rate"),
                                          f"Teknik hata artışının {dim} boyutunda tek segmente izole olup olmadığı test ediliyor.")
                if out is None:  # alternatif araç: yanıt kodu üzerinden kırılım
                    out_rc, ref = await run.call("analyze_response_codes", _pair(ctx, base_filters, dimension=dim),
                                                 f"breakdown_by_dimension başarısız; alternatif olarak yanıt kodu üzerinden {dim} kırılımı.")
                    out = (out_rc or {}).get("isolation")
                    if out_rc:
                        run.add_detections("analyze_response_codes", out_rc, ref)
                if out:
                    self._record_isolation(run, out, ref)
                    if out["isolated"]:
                        focus[dim] = out["isolated_segment"]
                        break
            codes, ref = await run.call("analyze_response_codes", _pair(ctx, focus),
                                        f"Etkilenen kapsamda ({_scope_text(focus)}) artan yanıt kodları inceleniyor.")
            if codes:
                run.add_detections("analyze_response_codes", codes, ref)
        elif sig["decline"]:
            codes, ref = await run.call("analyze_response_codes", _pair(ctx, base_filters, dimension="bank_id"),
                                        "Red oranı arttı; artan red kodu ve banka yoğunlaşması inceleniyor.")
            if codes:
                run.add_detections("analyze_response_codes", codes, ref)
                if codes.get("isolation"):
                    self._record_isolation(run, codes["isolation"], ref)
                    if codes["isolation"]["isolated"]:
                        focus["bank_id"] = codes["isolation"]["isolated_segment"]

        if sig["timeout"] or sig["latency"]:
            lat, ref = await run.call("analyze_latency_timeouts", _pair(ctx, focus),
                                      f"Gecikme ve timeout'ların aynı kapsamda ({_scope_text(focus)}) birlikte ve hangi sırayla arttığı ölçülüyor.")
            if lat:
                run.add_detections("analyze_latency_timeouts", lat, ref)
                corr = lat["detections"][0]
                if corr["detected"] and (lat["lead_minutes"] or 0) > 0:
                    run.hypotheses.append(AgentHypothesis(
                        text=f"Gecikme artışı timeout'lara katkıda bulunuyor olabilir (gecikme {lat['lead_minutes']} dk önce başladı); "
                             "bu bir korelasyondur, nedensellik kanıtı değildir.", evidence_refs=[ref]))

        service = focus.get("service_name") if "service_name" not in base_filters else None
        if service and sig["technical"]:
            other = {k: v for k, v in focus.items() if k not in ("service_name", "service_version")}
            ver, ref = await run.call("compare_service_versions", {"window": cw, "service_name": service, "filters": other},
                                      f"Sorun {service} servisinde yoğunlaşıyor; aynı penceredeki sürümler karşılaştırılıyor.")
            if ver and ver["detection"]["detected"]:
                d = ver["detection"]
                # Sürümlerin oranları ölçülmüş bir gözlemdir; sürümün hatalara yol açtığı ise hipotezdir
                run.findings.append(Finding(claim=f"Sürüm karşılaştırması (ölçülmüş): {d['evidence']}", detector=d["detector"],
                                            scope={k: str(v) for k, v in d["scope"].items()}, evidence_refs=[ref]))
                run.hypotheses.append(AgentHypothesis(
                    text=f"{service} {d['subject']} sürümü artan hatalarla ilişkili olabilir; değişiklik kayıtlarıyla doğrulanmalıdır.",
                    evidence_refs=[ref]))

        if sig["timeout"] and focus.get("channel", "POS") == "POS":
            run.follow_up.append(FollowUp(agent="payment_flow", reason="Timeout'lar koruyucu reversal üretir; reversal'sız timeout ve mutabakat kontrol edilmeli.",
                                          focus_filters={k: v for k, v in focus.items() if k not in ctx.filters}))
        run.metrics["focus"] = _scope_text(focus)
        names = sorted({f.detector for f in run.findings if f.detector})
        summary = (f"Tespitler: {', '.join(names)}; etkilenen kapsam: {_scope_text(focus)}." if names
                   else "Baseline'a göre eşik üzerinde sapma yok.")
        return run.result("success", summary, started)

    @staticmethod
    def _record_isolation(run: _Run, out: dict, ref: str) -> None:
        run.assertions.append(Assertion(dimension=out["dimension"], metric=out["metric"],
                                        isolated_segment=out["isolated_segment"], evidence_refs=[ref]))
        if out["isolated"]:
            run.findings.append(Finding(claim=f"Segmente izole sapma: {out['verdict']}", detector="SEGMENT_ISOLATION",
                                        scope={out["dimension"]: out["isolated_segment"]}, evidence_refs=[ref]))
            if out["dimension"] == "bank_id" and out["metric"] == "technical_rate":
                run.hypotheses.append(AgentHypothesis(
                    text=f"{out['isolated_segment']} issuer tarafında veya banka bağlantısında bir sorun olabilir.", evidence_refs=[ref]))


# ---------- Ödeme akışı ----------


class PaymentFlowAgent(SpecialistAgent):
    name = "payment_flow"
    description = "Authorization/sale/reversal/refund/timeout ilişkilerini correlation_id ile inceler; her reversal'ı hata saymaz."
    tools = frozenset({"check_data_coverage", "analyze_reversals", "breakdown_by_dimension", "search_transactions"})

    async def _investigate(self, run, task, ctx, store, started):
        filters = {**ctx.filters, **task.focus_filters}
        cw, _ = ctx.windows()

        # İlişki verisinin (correlation_id) varlığı: paylaşılan kanıtta yoksa ölç
        coverage = next((r["output"] for r in reversed(store.results) if r["tool"] == "check_data_coverage" and r.get("status") == "ok"
                         and r["output"]["filters"] == filters), None)
        if coverage is None:
            coverage, _ = await run.call("check_data_coverage", {"window": cw, "filters": filters},
                                         "Reversal bağlantısı için correlation_id alanının doluluğu kontrol ediliyor.")
        if coverage:
            null_rate = coverage["null_rates"].get("correlation_id") or 0
            if null_rate > 0.02:
                run.limitations.append(f"correlation_id alanının %{null_rate * 100:.1f}'i boş; reversal sınıflaması kısmi.")
            if coverage["evidence_quality"] == "insufficient":
                run.limitations += coverage["issues"]
                return run.result("insufficient_data", "Örneklem yetersiz; ödeme akışı analizi yapılmadı.", started)

        rev, ref = await run.call("analyze_reversals", _pair(ctx, filters),
                                  f"Reversal'lar orijinal işleme bağlanıp sınıflandırılıyor ({_scope_text(filters)}).")
        if rev is None:
            return run.result("failed", "Reversal analizi yapılamadı.", started)
        cats = rev["current"]["categories"]
        run.metrics.update({f"reversal_{k}": v for k, v in cats.items()})
        run.metrics.update(unreversed_timeouts=rev["current"]["unreversed_timeouts"], reconciliation_risk=rev["reconciliation_risk"]["level"])
        run.findings.append(Finding(
            claim=(f"Reversal sınıfları: meşru iptal {cats['void_after_approval']}, hata/timeout sonrası koruyucu {cats['after_failure']}, "
                   f"onay sonrası otomatik {cats['auto_after_approval']}, orphan {cats['orphan']}. Her reversal hata değildir; "
                   "yalnızca otomatik ve orphan reversal'lar teknik sayılır."), evidence_refs=[ref]))
        run.add_detections("analyze_reversals", rev, ref)
        if rev["current"]["unreversed_timeouts"]:
            run.findings.append(Finding(claim=f"{rev['current']['unreversed_timeouts']} POS timeout işleminin reversal kaydı yok "
                                              "(durumu belirsiz işlem, mutabakat riski).", evidence_refs=[ref]))
        if cats["after_failure"]:
            run.findings.append(Finding(claim=f"{cats['after_failure']} reversal, timeout veya hata alan işlemlere correlation_id ile bağlı; "
                                              "bu reversal'lar koruyucu akıştır ve ayrı bir anomali değildir.", evidence_refs=[ref]))

        status_filter = None
        merchant = None
        if rev["detection"]["detected"]:
            if "merchant_id" not in filters:
                br, bref = await run.call("breakdown_by_dimension", _pair(ctx, filters, dimension="merchant_id", metric="reversal_ratio"),
                                          "Teknik reversal artışının üye işyerine izole olup olmadığı test ediliyor.")
                if br:
                    run.assertions.append(Assertion(dimension="merchant_id", metric="reversal_ratio",
                                                    isolated_segment=br["isolated_segment"], evidence_refs=[bref]))
                    if br["isolated"]:
                        merchant = br["isolated_segment"]
                        run.findings.append(Finding(claim=f"Segmente izole sapma: {br['verdict']}", detector="SEGMENT_ISOLATION",
                                                    scope={"merchant_id": merchant}, evidence_refs=[bref]))
                        seg, sref = await run.call("analyze_reversals", _pair(ctx, {**filters, "merchant_id": merchant}),
                                                   f"Teknik reversal'lar {merchant} üye işyerinde yoğunlaşıyor; sınıflama bu segmentte tekrarlanıyor.")
                        if seg:
                            run.findings.append(Finding(claim=f"{merchant}: {seg['detection']['evidence']}", evidence_refs=[sref]))
            auto = cats["auto_after_approval"]
            if auto >= cats["orphan"]:
                run.hypotheses.append(AgentHypothesis(
                    text=(f"Terminaller{' (' + merchant + ')' if merchant else ''} onay yanıtını alamıyor olabilir; onaylı işlemler saniyeler "
                          "içinde otomatik geri alınıyor. Bu bir hipotezdir; terminal kayıtlarıyla doğrulanmalıdır."), evidence_refs=[ref]))
            if merchant and task.round == 1:
                run.follow_up.append(FollowUp(
                    agent="anomaly_detection", focus_filters={"merchant_id": merchant},
                    reason=f"Teknik reversal'lar {merchant}'de yoğunlaşıyor; bu üye işyerinde teknik hata, timeout veya gecikme artışı var mı?"))
        elif rev["current"]["unreversed_timeouts"] or cats["after_failure"]:
            status_filter = "timeout"

        sample_filters = {**filters, **({"merchant_id": merchant} if merchant else {}),
                          **({"transaction_type": "reversal"} if status_filter is None else {})}
        tx, tref = await run.call("search_transactions", {"window": cw, "filters": sample_filters, "transaction_status": status_filter, "limit": 5},
                                  "İlgili akıştan maskelenmiş örnek işlemler alınıyor.")
        if tx and tx["total_matches"]:
            run.findings.append(Finding(claim=f"Filtreye uyan {tx['total_matches']} işlem var; örnek: "
                                              f"{', '.join(s['transaction_id'] for s in tx['samples'][:3])}.", evidence_refs=[tref]))
        rr = rev["reconciliation_risk"]
        summary = (f"Reversal analizi: teknik reversal {'artışı var' if rev['detection']['detected'] else 'artışı yok'}"
                   f"{' (' + merchant + ')' if merchant else ''}; mutabakat riski göstergesi {rr['level']}.")
        return run.result("success", summary, started)


# ---------- Geçmiş olaylar ve runbook'lar ----------


class HistoricalIncidentAgent(SpecialistAgent):
    name = "historical_incident"
    description = "Geçmiş incident'ları ve runbook'ları arar; güvenilir eşleşme yoksa bunu açıkça söyler."
    tools = frozenset({"search_similar_incidents", "search_runbooks"})

    async def _investigate(self, run, task, ctx, store, started):
        sim, ref = await run.call("search_similar_incidents", _pair(ctx, dict(ctx.filters)),
                                  "Anomali imzası (araç çıktılarından) geçmiş incident kayıtlarıyla karşılaştırılıyor.")
        if sim is not None:
            if sim["matches"]:
                for m in sim["matches"]:
                    run.findings.append(Finding(
                        claim=(f"Geçmiş olay {m['incident_id']} benzer belirtili (benzerlik {m['similarity']}; "
                               f"{'; '.join(m['why_similar'][:2])})."), evidence_refs=[ref]))
                top = sim["matches"][0]
                run.metrics.update(top_incident=top["incident_id"], top_similarity=top["similarity"])
                if top["established_factors"]:
                    run.hypotheses.append(AgentHypothesis(
                        text=(f"{top['incident_id']} olayında doğrulanan faktör ({'; '.join(top['established_factors'])}) burada da "
                              "etkili olabilir; bu olay için doğrulanmadı."), evidence_refs=[ref]))
            else:
                run.findings.append(Finding(claim=f"Güvenilir bir geçmiş olay eşleşmesi yok: {sim['message']}", evidence_refs=[ref]))

        # Runbook sorgusu LLM metninden değil, paylaşılan kanıttaki deterministik tespitlerden kurulur
        found = [d for r in store.results if r.get("status") == "ok" for d in detections_of(r["tool"], r["output"]) if d["detected"]]
        query = runbook_query(found)
        rb, rref = await run.call("search_runbooks", {"query": query}, f"İlgili runbook aranıyor: '{query}'.")
        if rb is not None:
            if rb["found"]:
                sections = sorted({f"{h['source']} › {h['section'].split(' › ')[-1]}" for h in rb["hits"]})
                run.findings.append(Finding(claim=f"İlgili runbook bölümleri: {'; '.join(sections)}.", evidence_refs=[rref]))
                if any(h["flagged"] for h in rb["hits"]):
                    run.limitations.append("Bir runbook parçasında doküman içi talimat bulundu ve çıkarıldı; içerik veri olarak ele alındı.")
            else:
                run.findings.append(Finding(claim="İlgili runbook bulunamadı; öneriler yalnızca gözlemlere dayanacak.", evidence_refs=[rref]))
        if sim is None and rb is None:
            return run.result("failed", "Geçmiş olay ve runbook araması yapılamadı.", started)
        summary = (f"{len(sim['matches'])} benzer geçmiş olay" if sim and sim["matches"] else "Benzer geçmiş olay yok") + \
                  (f"; runbook: {len(rb['hits'])} bölüm." if rb and rb["found"] else "; runbook eşleşmesi yok.")
        return run.result("success", summary, started)


# ---------- Etki ve risk ----------


class ImpactRiskAgent(SpecialistAgent):
    name = "impact_risk"
    description = "Doğrulanmış metriklerle deterministik risk, etkilenen işlem, kapsam ve süreyi hesaplar; parasal kayıp üretmez."
    tools = frozenset()  # veri sorgusu yapmaz; paylaşılan kanıt üzerinde tekrarlanabilir hesap yapar

    async def _investigate(self, run, task, ctx, store, started):
        scope = {"current_start": ctx.time_range.current_start, "current_end": ctx.time_range.current_end,
                 "baseline_start": ctx.baseline_definition.start, "baseline_end": ctx.baseline_definition.end,
                 "filters": ctx.filters}
        inputs = store.ok_refs()
        failures = sum(1 for r in store.results if r.get("error_type") in ("tool", "transient"))
        risk = assess_risk(store.results, scope, tool_failures=failures)
        ref = store.record({"step": None, "agent": self.name, "round": task.round, "tool": "assess_impact",
                            "args": {"inputs": inputs}, "scope": _scope_text(ctx.filters), "status": "ok",
                            "output": risk.model_dump(), "attempts": 1, "duration_ms": 0.0,
                            "decision_reason": "Risk, paylaşılan kanıttaki araç çıktılarından deterministik olarak hesaplanıyor.",
                            "summary": f"risk {risk.severity}" + (f", skor {risk.score}" if risk.score is not None else "")})
        run.refs.append(ref)
        run.metrics.update(severity=risk.severity, score=risk.score, affected_transactions=risk.affected_transactions,
                           evidence_quality=risk.evidence_quality, confidence=risk.confidence, inputs=len(inputs))
        run.findings.append(Finding(
            claim=(f"Deterministik risk: {risk.severity}" + (f" (skor {risk.score}/100)" if risk.score is not None else "")
                   + f"; etkilenen işlem ~{risk.affected_transactions}; kanıt kalitesi {risk.evidence_quality}. {risk.monetary_impact}"),
            evidence_refs=[ref]))
        for factor in risk.factors:
            run.findings.append(Finding(claim=f"Risk faktörü {factor.factor}: {factor.value} → +{factor.points}", evidence_refs=[ref]))
        run.limitations += risk.evidence_limitations
        if risk.severity == "INDETERMINATE":
            return run.result("insufficient_data", "Kanıt yetersiz; risk seviyesi belirlenemedi.", started)
        return run.result("success", f"Risk {risk.severity}, skor {risk.score}, etkilenen işlem ~{risk.affected_transactions}.", started)


def default_agents() -> dict[str, SpecialistAgent]:
    return {a.name: a for a in (AnomalyDetectionAgent(), PaymentFlowAgent(), HistoricalIncidentAgent(), ImpactRiskAgent())}
