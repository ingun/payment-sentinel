"""
Sentetik ödeme logları üretir. Sabit seed sayesinde her çalıştırmada aynı veri oluşur.

Çalıştırma: python -m src.data_generator
"""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import SAMPLE_LOGS_PATH, SCENARIO_DIR, SERVICES

SEED = 42
START_TIME = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
DURATION_MIN = 180

SERVICE_WEIGHTS = [0.15, 0.25, 0.25, 0.25, 0.10]
BASE_LATENCY_MS = {
    "api-gateway": 120,
    "payment-orchestrator": 180,
    "acquirer-gateway": 250,
    "provider-connector": 300,
    "fraud-check": 90,
}
PAYMENT_METHODS = ["card", "wallet", "bank_transfer"]
PAYMENT_METHOD_WEIGHTS = [0.7, 0.2, 0.1]

NORMAL_FAILURE_PROB = 0.02
NORMAL_ERROR_CODES = {
    "CARD_DECLINED": 0.45,
    "INSUFFICIENT_FUNDS": 0.30,
    "HTTP_500": 0.10,
    "PROVIDER_TIMEOUT": 0.10,
    "CONNECTION_RESET": 0.05,
}

SCENARIOS = {
    "normal": "Normal ödeme trafiği",
    "error_spike": "provider-connector'da PROVIDER_TIMEOUT artışı (11:35 sonrası)",
    "latency_degradation": "acquirer-gateway'de gecikme artışı ve GATEWAY_504 (11:25 sonrası)",
    "volume_drop": "İşlem hacminde ~%60 düşüş, hata oranı normal (11:40 sonrası)",
}

ERROR_SPIKE_START_MIN = 155
VOLUME_DROP_START_MIN = 160
LATENCY_START_MIN = 145
LATENCY_RAMP_MIN = 15


def _weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _latency(rng: random.Random, mean_ms: float) -> int:
    return max(20, int(rng.gauss(mean_ms, mean_ms * 0.25)))


def _record(rng: random.Random, scenario: str, minute: int, index: int, ts: datetime) -> dict:
    service = rng.choices(SERVICES, weights=SERVICE_WEIGHTS)[0]
    latency_mean = BASE_LATENCY_MS[service]
    failure_prob = NORMAL_FAILURE_PROB
    error_codes = NORMAL_ERROR_CODES

    if scenario == "error_spike" and minute >= ERROR_SPIKE_START_MIN and service == "provider-connector":
        failure_prob = 0.55
        error_codes = {"PROVIDER_TIMEOUT": 0.82, "CONNECTION_RESET": 0.13, "ERR_PRV_7781": 0.05}

    if scenario == "latency_degradation" and minute >= LATENCY_START_MIN and service == "acquirer-gateway":
        ramp = min(1.0, (minute - LATENCY_START_MIN + 1) / LATENCY_RAMP_MIN)
        latency_mean = BASE_LATENCY_MS[service] + ramp * 1600
        failure_prob = 0.05 + ramp * 0.22
        error_codes = {"GATEWAY_504": 0.8, "HTTP_502": 0.2}

    failed = rng.random() < failure_prob
    error_code = _weighted_choice(rng, error_codes) if failed else None
    latency = _latency(rng, latency_mean)
    if error_code in {"PROVIDER_TIMEOUT", "GATEWAY_504"}:
        latency = 5000 + rng.randint(0, 400)  # istemci timeout süresi

    return {
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "transaction_id": f"TX-{scenario[:3].upper()}-{index:06d}",
        "service": service,
        "status": "failed" if failed else "success",
        "error_code": error_code,
        "latency_ms": latency,
        "payment_method": rng.choices(PAYMENT_METHODS, weights=PAYMENT_METHOD_WEIGHTS)[0],
        "environment": "prod-sim",
    }


def generate_scenario(scenario: str, seed: int = SEED) -> list[dict]:
    if scenario not in SCENARIOS:
        raise ValueError(f"Bilinmeyen senaryo: {scenario}")
    rng = random.Random(f"{seed}-{scenario}")
    records: list[dict] = []
    for minute in range(DURATION_MIN):
        minute_start = START_TIME + timedelta(minutes=minute)
        count = rng.randint(10, 14)
        if scenario == "volume_drop" and minute >= VOLUME_DROP_START_MIN:
            count = rng.randint(4, 6)
        offsets = sorted(rng.uniform(0, 60) for _ in range(count))
        for offset in offsets:
            ts = (minute_start + timedelta(seconds=offset)).replace(microsecond=0)
            records.append(_record(rng, scenario, minute, len(records) + 1, ts))
    return records


def write_jsonl(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    for scenario in SCENARIOS:
        records = generate_scenario(scenario)
        write_jsonl(records, SCENARIO_DIR / f"{scenario}.jsonl")
        print(f"{scenario}: {len(records)} kayıt yazıldı")
    write_jsonl(generate_scenario("error_spike"), SAMPLE_LOGS_PATH)
    print(f"Varsayılan örnek: {SAMPLE_LOGS_PATH.name} (error_spike)")


if __name__ == "__main__":
    main()
