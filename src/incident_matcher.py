"""
Güncel anomali imzasını sentetik geçmiş incident kayıtlarıyla karşılaştırır.

Benzerlik yapısal özellikler (dedektör tipleri, etkilenen kapsam, yanıt kodları, metrik büyüklükleri)
ile metin benzerliğinin (BM25, belirti özeti ↔ incident özeti) ağırlıklı birleşimidir. Her eşleşme neden
benzer bulunduğunu ve farklarını döner. Benzer belirti, aynı nedenin mevcut olduğunu kanıtlamaz.
"""

import math

from rank_bm25 import BM25Okapi

from src.repositories.base import HistoricalIncidentRepository
from src.retriever import tokenize

WEIGHTS = {"detectors": 0.30, "scope": 0.20, "response_codes": 0.20, "metrics": 0.10, "text": 0.20}
METRIC_SCALES = {"technical_delta_pp": 2.0, "decline_delta_pp": 2.0, "p95_change_pct": 50.0, "technical_reversals": 10.0}
MIN_SIMILARITY = 0.55
TOP_K = 3
DISCLAIMER = ("Geçmiş bir olayla benzerlik yalnızca belirti benzerliğidir; aynı nedenin mevcut olduğunu kanıtlamaz. "
              "Geçmiş kayıttaki 'doğrulanmış' faktörler o olay için doğrulanmıştır, bu olay için değil.")
PART_LABELS = {"detectors": "tespit tipleri", "scope": "etkilenen kapsam", "response_codes": "yanıt kodları",
               "metrics": "metrik büyüklükleri", "text": "belirti metni"}


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def closeness(a: float | None, b: float | None, scale: float) -> float:
    """İki değerin yakınlığı (0-1); büyük değerler için logaritmik ölçek."""
    if a is None or b is None:
        return 0.0

    def squash(x: float) -> float:
        return math.copysign(math.log1p(abs(x) / scale), x)

    return max(0.0, 1 - abs(squash(a) - squash(b)) / 2)


def scope_similarity(sig_scope: dict, inc_scope: dict) -> float:
    if not sig_scope:
        return 0.5 if not inc_scope else 0.0
    parts = []
    for key, value in sig_scope.items():
        if key not in inc_scope:
            parts.append(0.0)
        else:
            parts.append(0.5 + (0.5 if inc_scope[key] == value else 0.0))
    return sum(parts) / len(parts)


def symptom_text(signature: dict) -> str:
    words = list(signature["detectors"]) + list(signature["response_codes"])
    words += [f"{k} {v}" for k, v in signature["scope"].items()]
    return " ".join(words + [signature.get("description", "")])


def score(signature: dict, incident: dict, text_score: float) -> dict:
    metrics = [closeness(signature["metrics"].get(k), incident["metrics"].get(k), s) for k, s in METRIC_SCALES.items()]
    parts = {
        "detectors": jaccard(set(signature["detectors"]), set(incident["detectors"])),
        "scope": scope_similarity(signature["scope"], incident.get("scope", {})),
        "response_codes": jaccard(set(signature["response_codes"]), set(incident["response_codes"])),
        "metrics": sum(metrics) / len(metrics),
        "text": text_score,
    }
    total = round(sum(WEIGHTS[k] * v for k, v in parts.items()), 3)
    why = [f"{PART_LABELS[k]} benzer ({v:.2f})" for k, v in parts.items() if v >= 0.6]
    diff = [f"{PART_LABELS[k]} farklı ({v:.2f})" for k, v in parts.items() if v < 0.4]
    shared = sorted(set(signature["detectors"]) & set(incident["detectors"]))
    if shared:
        why.insert(0, "ortak tespitler: " + ", ".join(shared))
    return {"similarity": total, "breakdown": {k: round(v, 2) for k, v in parts.items()}, "why_similar": why,
            "differences": diff}


def find_similar(signature: dict, repository: HistoricalIncidentRepository, top_k: int = TOP_K,
                 min_similarity: float = MIN_SIMILARITY) -> dict:
    base = {"signature": signature, "disclaimer": DISCLAIMER}
    if not signature["detectors"]:
        return {**base, "matches": [], "found": False,
                "message": "Tespit edilen anomali yok; geçmiş olay karşılaştırması yapılmadı."}
    incidents = repository.all()
    if not incidents:
        return {**base, "matches": [], "found": False, "message": "Geçmiş incident kaydı yok."}
    corpus = [tokenize(f"{i['summary']} {' '.join(i['detectors'])} {' '.join(i['response_codes'])} "
                       f"{' '.join(f'{k} {v}' for k, v in i.get('scope', {}).items())}") for i in incidents]
    raw = BM25Okapi(corpus).get_scores(tokenize(symptom_text(signature)))
    top = max(raw) if len(raw) and max(raw) > 0 else 0
    scored = []
    for incident, text_raw in zip(incidents, raw):
        if not set(signature["detectors"]) & set(incident["detectors"]):
            continue  # ortak tespit tipi olmayan olay benzer sayılmaz
        result = score(signature, incident, round(max(0.0, float(text_raw)) / float(top), 3) if top else 0.0)
        if result["similarity"] < min_similarity:
            continue
        factors = incident.get("contributing_factors", [])
        scored.append({
            "incident_id": incident["incident_id"], "timestamp": incident["timestamp"], "service": incident["service"],
            "scope": incident.get("scope", {}), "response_codes": incident["response_codes"],
            "summary": incident["summary"],
            "established_factors": [f["factor"] for f in factors if f.get("established")],
            "unconfirmed_factors": [f["factor"] for f in factors if not f.get("established")],
            "resolution_steps": incident["resolution_steps"], "outcome": incident["outcome"],
            "match_type": "similar_symptom", **result,
        })
    scored.sort(key=lambda m: (-m["similarity"], m["incident_id"]))
    matches = scored[:top_k]
    message = (f"{len(matches)} benzer belirtili geçmiş olay bulundu." if matches
               else "Eşik üzerinde benzer geçmiş olay bulunamadı; eşleşme uydurulmadı.")
    return {**base, "matches": matches, "found": bool(matches), "message": message}
