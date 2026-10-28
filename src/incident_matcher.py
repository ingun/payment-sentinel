"""
Güncel anomali imzasını sentetik geçmiş incident kayıtlarıyla karşılaştırır.

Benzerlik yalnızca hata koduna değil; servis, patern tipleri, başlangıç şekli,
hata oranı farkı, gecikme ve hacim değişimine dayanır. Benzerlik, aynı kök nedenin
mevcut olduğu anlamına gelmez.
"""

import json
import math
from pathlib import Path

from src.config import DATA_DIR

INCIDENTS_PATH = DATA_DIR / "incidents.json"

WEIGHTS = {
    "service": 0.20,
    "error_codes": 0.20,
    "pattern_types": 0.25,
    "onset_shape": 0.10,
    "error_rate_delta": 0.10,
    "latency_change": 0.10,
    "volume_change": 0.05,
}
ERROR_PATTERN_TYPES = {
    "ERROR_RATE_SPIKE",
    "ERROR_CODE_CONCENTRATION",
    "SERVICE_ERROR_SPIKE",
    "UNKNOWN_ERROR_CODE",
    "ERROR_LATENCY_COOCCURRENCE",
}
MIN_SIMILARITY = 0.45
TOP_K = 3
DISCLAIMER = "Geçmiş bir olayla benzerlik, aynı kök nedenin mevcut olduğunu kanıtlamaz; yalnızca inceleme ipucudur."


def load_incidents(path: Path = INCIDENTS_PATH) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def closeness(a: float | None, b: float | None, scale: float) -> float:
    """İki değerin yakınlığı (0-1). Büyük yüzdeler için logaritmik ölçek kullanılır."""
    if a is None or b is None:
        return 0.0

    def squash(x: float) -> float:
        return math.copysign(math.log1p(abs(x) / scale), x)

    return max(0.0, 1 - abs(squash(a) - squash(b)) / 2)


def signature_from_analysis(analysis: dict, comparison: dict) -> dict:
    """Patern analizinden karşılaştırılabilir bir anomali imzası çıkarır."""
    services = analysis.get("services", [])
    affected = analysis.get("affected_services", [])
    primary = affected[0] if affected else None
    if len(affected) > 1:
        ranked = sorted((s for s in services if s["service"] in affected), key=lambda s: s["failed"], reverse=True)
        primary = ranked[0]["service"]
    # Hata kodları yalnızca hata kaynaklı bir patern varsa imzaya girer
    error_related = set(analysis.get("detected_types", [])) & ERROR_PATTERN_TYPES
    codes = [c["key"] for c in analysis.get("top_error_codes", []) if c["share"] >= 0.1] if error_related else []
    service_p95 = next((s["p95_change_pct"] for s in services if s["service"] == primary), None)
    return {
        "service": primary,
        "error_codes": codes,
        "pattern_types": analysis.get("detected_types", []),
        "onset_shape": analysis.get("onset_shape"),
        "error_rate_delta_pp": comparison.get("error_rate_delta_pp"),
        "p95_change_pct": service_p95 if service_p95 is not None else comparison.get("p95_change_pct"),
        "volume_change_pct": comparison.get("volume_change_pct"),
    }


def score(signature: dict, incident: dict) -> dict:
    parts = {
        "service": 1.0 if signature["service"] and signature["service"] == incident["service"] else 0.0,
        "error_codes": jaccard(set(signature["error_codes"]), set(incident["error_codes"])),
        "pattern_types": jaccard(set(signature["pattern_types"]), set(incident["pattern_types"])),
        "onset_shape": 1.0 if signature["onset_shape"] and signature["onset_shape"] == incident["onset_shape"] else 0.0,
        "error_rate_delta": closeness(signature["error_rate_delta_pp"], incident["error_rate_delta_pp"], 2.0),
        "latency_change": closeness(signature["p95_change_pct"], incident["p95_change_pct"], 50.0),
        "volume_change": closeness(signature["volume_change_pct"], incident["volume_change_pct"], 20.0),
    }
    total = round(sum(WEIGHTS[k] * v for k, v in parts.items()), 3)
    return {"similarity": total, "breakdown": {k: round(v, 2) for k, v in parts.items()}}


def find_similar(signature: dict, incidents: list[dict], top_k: int = TOP_K, min_similarity: float = MIN_SIMILARITY) -> dict:
    if not signature["pattern_types"]:
        return {"signature": signature, "matches": [], "found": False,
                "message": "Tespit edilen patern yok; geçmiş olay karşılaştırması yapılmadı.", "disclaimer": DISCLAIMER}
    scored = []
    for incident in incidents:
        result = score(signature, incident)
        if result["similarity"] >= min_similarity:
            scored.append({
                "incident_id": incident["incident_id"],
                "service": incident["service"],
                "error_codes": incident["error_codes"],
                "cause": incident["cause"],
                "cause_status": incident["cause_status"],
                "resolution": incident["resolution"],
                "outcome": incident["outcome"],
                **result,
            })
    scored.sort(key=lambda m: m["similarity"], reverse=True)
    matches = scored[:top_k]
    message = f"{len(matches)} benzer geçmiş olay bulundu." if matches else "Eşik üzerinde benzer geçmiş olay bulunamadı."
    return {"signature": signature, "matches": matches, "found": bool(matches), "message": message, "disclaimer": DISCLAIMER}
