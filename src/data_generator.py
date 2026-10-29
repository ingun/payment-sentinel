"""
Sentetik ödeme olayları üretir. TÜM VERİLER SENTETİKTİR; üretim ölçümü değildir.

- İki gün aynı saat aralığı üretilir: D-1 ve D (06:00-12:00 UTC). D-1 her senaryoda normaldir
  ve gün içi sezonsallık için "dünün aynı penceresi" baseline'ı olarak kullanılır.
- Reversal kayıtları orijinal işlemin correlation_id'sini taşır; orphan reversal'ların orijinali yoktur.
- Sabit seed: aynı (senaryo, seed) çifti her zaman aynı veriyi üretir.

Çalıştırma (inceleme için JSONL dışa aktarımı): python -m src.data_generator --export
"""

import argparse
import json
import math
import random
import zlib
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import numpy as np

from src.config import GENERATED_DIR
from src.domain import BANKS, ECOM_MERCHANTS, POS_MERCHANTS, SYNTHETIC_LABEL, status_for

SEED = 42
DAY = datetime(2026, 10, 27, tzinfo=timezone.utc)
DAY_START_HOUR, DAY_END_HOUR = 6, 12
PEAK_PER_MIN = 25.0
CLIENT_TIMEOUT_MS = 10_000

BANK_WEIGHTS = [0.30, 0.25, 0.20, 0.15, 0.10]
BANK_LATENCY_FACTOR = {"BANK_A": 1.0, "BANK_B": 1.1, "BANK_C": 0.95, "BANK_D": 1.2, "BANK_E": 1.05}
SERVICE_MEDIAN_MS = {"pos-gateway": 320, "payment-orchestrator": 450, "ecom-gateway": 380}
SERVICE_VERSION = {"pos-gateway": "3.8.2", "payment-orchestrator": "2.3.7", "ecom-gateway": "1.12.0"}

TECHNICAL_PROBS = {"96": 0.002, "91": 0.002, "68": 0.002}
DECLINE_PROBS = {"05": 0.025, "51": 0.025, "54": 0.004, "55": 0.004, "57": 0.003, "14": 0.002}
REFUND_SHARE = 0.012
VOID_PROB = 0.008                # onaylı satıştan sonra meşru iptal (void)
AUTO_REVERSAL_PROB = 0.0005      # onaydan hemen sonra teknik otomatik reversal (normalde çok nadir)
TIMEOUT_REVERSAL_PROB = 0.85     # POS'ta timeout sonrası koruyucu reversal
ORPHAN_PER_MIN = 0.01


def _t(hour: int, minute: int = 0) -> int:
    """06:00'dan itibaren dakika."""
    return (hour - DAY_START_HOUR) * 60 + minute


SCENARIOS = {
    "normal_baseline": "Normal ödeme trafiği (referans)",
    "response_code_spike": "BANK_C'de 05 (do not honor) red kodunda artış (11:10 sonrası)",
    "bank_timeout": "Yalnızca BANK_B'de timeout (68/91) ve gecikme artışı (11:15 sonrası)",
    "latency_then_failures": "pos-gateway'de kademeli gecikme (11:00), ardından timeout'lar (11:20)",
    "reversal_anomaly": "M-007 terminallerinde onay sonrası otomatik reversal ve orphan reversal artışı (11:10 sonrası)",
    "version_regression": "payment-orchestrator 2.4.0 canary sürümünde 96 hatası (10:45 deploy)",
    "low_volume": "Çok düşük hacim; birkaç hata yüksek oran gibi görünür ama kanıt yetersiz",
    "normal_fluctuation": "Normal dalgalanma: daha yüksek sabah zirvesi ve kısa gürültü (yüksek alarm beklenmez)",
    "volume_drop": "İşlem hacminde ~%60 düşüş, hata oranı normal (11:10 sonrası)",
    "data_quality_gap": "Log hattı sorunu: 11:20 sonrası alan kaybı (bank_id, sürüm, correlation_id) ve 11:35-11:50 veri boşluğu",
}

# Değerlendirme için beklenen sonuçlar (ground truth). scope boşsa yalnızca dedektör tipi beklenir.
SCENARIO_LABELS: dict[str, dict] = {
    "normal_baseline": {"expected": [], "severities": ["LOW"], "insufficient": False,
                        "request": "Son 1 saatteki ödeme hatalarını kontrol et."},
    "response_code_spike": {
        "expected": [{"detector": "DECLINE_RATE_SPIKE"},
                     {"detector": "RESPONSE_CODE_CONCENTRATION", "scope": {"response_code": "05"}},
                     {"detector": "SEGMENT_ISOLATION", "scope": {"bank_id": "BANK_C"}}],
        "severities": ["MEDIUM", "HIGH"], "insufficient": False,
        "request": "Son 1 saatte red (decline) oranları neden arttı? Belirli bir bankaya mı özgü?"},
    "bank_timeout": {
        "expected": [{"detector": "TECHNICAL_FAILURE_SPIKE"}, {"detector": "TIMEOUT_RATE_SPIKE"},
                     {"detector": "SEGMENT_ISOLATION", "scope": {"bank_id": "BANK_B"}},
                     {"detector": "RESPONSE_CODE_CONCENTRATION", "scope": {"response_code": "68"}},
                     {"detector": "LATENCY_INCREASE"}, {"detector": "LATENCY_TIMEOUT_CORRELATION"}],
        # 91 de enjekte edilir (%7); tespit edilmesi doğrudur ama her seed'de eşiği geçmesi beklenmez
        "acceptable": [{"detector": "RESPONSE_CODE_CONCENTRATION", "scope": {"response_code": "91"}}],
        "severities": ["HIGH"], "insufficient": False,
        "request": "Son 1 saatteki POS başarısızlıklarındaki artışı araştır."},
    "latency_then_failures": {
        "expected": [{"detector": "LATENCY_INCREASE"}, {"detector": "TIMEOUT_RATE_SPIKE"},
                     {"detector": "TECHNICAL_FAILURE_SPIKE"},
                     {"detector": "SEGMENT_ISOLATION", "scope": {"service_name": "pos-gateway"}},
                     {"detector": "RESPONSE_CODE_CONCENTRATION", "scope": {"response_code": "68"}},
                     {"detector": "LATENCY_TIMEOUT_CORRELATION"}],
        "severities": ["MEDIUM", "HIGH"], "insufficient": False,
        "request": "Son 1 saatte gecikme ve timeout'lar arasında ilişki var mı?"},
    "reversal_anomaly": {
        "expected": [{"detector": "REVERSAL_ANOMALY"},
                     {"detector": "SEGMENT_ISOLATION", "scope": {"merchant_id": "M-007"}}],
        "severities": ["MEDIUM", "HIGH"], "insufficient": False,
        "request": "Son 1 saatte reversal dağılımı değişti mi? Mutabakat riski var mı?"},
    "version_regression": {
        "expected": [{"detector": "TECHNICAL_FAILURE_SPIKE"},
                     {"detector": "SEGMENT_ISOLATION", "scope": {"service_name": "payment-orchestrator"}},
                     {"detector": "RESPONSE_CODE_CONCENTRATION", "scope": {"response_code": "96"}},
                     {"detector": "SERVICE_VERSION_DEGRADATION", "scope": {"service_version": "2.4.0"}}],
        "severities": ["MEDIUM", "HIGH"], "insufficient": False,
        "request": "Son 1 saatteki ödeme hatalarını araştır; bir deployment ile ilişkili olabilir mi?"},
    "low_volume": {"expected": [], "severities": ["INDETERMINATE"], "insufficient": True,
                   "request": "Son 30 dakikadaki ödeme hatalarını araştır."},
    "normal_fluctuation": {"expected": [], "severities": ["LOW", "MEDIUM"], "insufficient": False,
                           "request": "Bu sabah trafikte olağandışı bir şey var mı?"},
    "volume_drop": {"expected": [{"detector": "VOLUME_ANOMALY"}], "severities": ["MEDIUM", "HIGH"], "insufficient": False,
                    "request": "Son 1 saatte işlem hacminde düşüş var mı?"},
    # Veri kalitesi sorunu anomali değildir: güçlü iddia beklenmez, kanıt kalitesi "limited" olmalıdır
    "data_quality_gap": {"expected": [], "severities": ["LOW", "MEDIUM"], "insufficient": False, "evidence_quality": "limited",
                         "request": "Son 1 saatteki ödeme hatalarını ve reversal'ları kontrol et."},
}

# Senaryo parametreleri
RC_SPIKE_START, RC_SPIKE_BANK, RC_SPIKE_PROB = _t(11, 10), "BANK_C", 0.25
BANK_TIMEOUT_START, BANK_TIMEOUT_BANK = _t(11, 15), "BANK_B"
LATENCY_START, LATENCY_RAMP_MIN, LATENCY_EXTRA_MS = _t(11, 0), 30, 2200
LATENCY_FAIL_START, LATENCY_FAIL_RAMP_MIN, LATENCY_FAIL_MAX = _t(11, 20), 20, 0.15
REVERSAL_START, REVERSAL_MERCHANT, REVERSAL_PROB, REVERSAL_ORPHAN_PER_MIN = _t(11, 10), "M-007", 0.4, 0.3
DEPLOY_START, CANARY_VERSION, CANARY_SHARE, CANARY_ERROR_PROB = _t(10, 45), "2.4.0", 0.4, 0.22
LOW_VOLUME_PER_MIN, LOW_VOLUME_FAIL_START, LOW_VOLUME_FAIL_PROB = 0.5, _t(11, 35), 0.15
FLUCTUATION_FACTOR, FLUCTUATION_BURST = 1.15, (_t(11, 30), _t(11, 35), 0.03)
VOLUME_DROP_START, VOLUME_DROP_FACTOR = _t(11, 10), 0.4
DQ_FIELD_LOSS_START, DQ_FIELD_LOSS_SHARE, DQ_LOST_FIELDS = _t(11, 20), 0.25, ("bank_id", "service_version", "correlation_id")
DQ_GAP = (_t(11, 35), _t(11, 50))  # olay toplama boşluğu: bu dakikalarda hiç kayıt yok


def arrival_rate(minute: int, scenario: str, today: bool) -> float:
    """Dakika başı beklenen işlem: sabah rampası, 09:30 civarında zirve."""
    if scenario == "low_volume":
        return LOW_VOLUME_PER_MIN
    shape = 0.35 + 0.65 * min(1.0, minute / 210)
    rate = PEAK_PER_MIN * shape
    if today and scenario == "normal_fluctuation":
        rate *= FLUCTUATION_FACTOR
    if today and scenario == "volume_drop" and minute >= VOLUME_DROP_START:
        rate *= VOLUME_DROP_FACTOR
    if today and scenario == "data_quality_gap" and DQ_GAP[0] <= minute < DQ_GAP[1]:
        rate = 0.0
    return rate


class _Ids:
    def __init__(self, prefix: str):
        self.prefix, self.n = prefix, 0

    def tx(self) -> str:
        self.n += 1
        return f"SYN-TX-{self.prefix}-{self.n:07d}"

    def corr(self) -> str:
        return f"SYN-COR-{self.prefix}-{self.n:07d}"


def _choose(rng: random.Random, probs: dict[str, float]) -> str | None:
    r = rng.random()
    for code, p in probs.items():
        if r < p:
            return code
        r -= p
    return None


def _latency(rng: random.Random, median: float) -> int:
    return max(30, min(CLIENT_TIMEOUT_MS - 100, int(median * math.exp(rng.gauss(0, 0.35)))))


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _primary(rng: random.Random, ids: _Ids, scenario: str, today: bool, minute: int, ts: datetime) -> list[dict]:
    channel = "POS" if rng.random() < 0.65 else "ECOM"
    bank = rng.choices(BANKS, weights=BANK_WEIGHTS)[0]
    if channel == "POS":
        merchant = rng.choice(POS_MERCHANTS)
        terminal = f"T-{merchant[2:]}-{rng.randint(1, 4)}"
        method = "card_present" if rng.random() < 0.55 else "contactless"
        service = "pos-gateway"
        tx_type = "sale" if rng.random() < 0.85 else "authorization"
    else:
        merchant = rng.choice(ECOM_MERCHANTS)
        terminal = f"VT-{merchant[2:]}"
        method = "ecom_card" if rng.random() < 0.7 else "wallet"
        service = "payment-orchestrator" if method == "ecom_card" else "ecom-gateway"
        tx_type = "authorization" if rng.random() < 0.6 else "sale"
    version = SERVICE_VERSION[service]
    median = SERVICE_MEDIAN_MS[service] * BANK_LATENCY_FACTOR[bank]
    technical, declines = dict(TECHNICAL_PROBS), dict(DECLINE_PROBS)
    auto_reversal_prob = AUTO_REVERSAL_PROB

    if today:
        if scenario == "response_code_spike" and minute >= RC_SPIKE_START and bank == RC_SPIKE_BANK:
            declines["05"] += RC_SPIKE_PROB
        if scenario == "bank_timeout" and minute >= BANK_TIMEOUT_START and bank == BANK_TIMEOUT_BANK:
            technical.update({"68": 0.18, "91": 0.07})
            median *= 3.5
        if scenario == "latency_then_failures" and minute >= LATENCY_START and service == "pos-gateway":
            median += min(1.0, (minute - LATENCY_START + 1) / LATENCY_RAMP_MIN) * LATENCY_EXTRA_MS
            if minute >= LATENCY_FAIL_START:
                technical["68"] += min(1.0, (minute - LATENCY_FAIL_START + 1) / LATENCY_FAIL_RAMP_MIN) * LATENCY_FAIL_MAX
        if scenario == "reversal_anomaly" and minute >= REVERSAL_START and merchant == REVERSAL_MERCHANT:
            auto_reversal_prob = REVERSAL_PROB
        if scenario == "version_regression" and minute >= DEPLOY_START and service == "payment-orchestrator" \
                and rng.random() < CANARY_SHARE:
            version = CANARY_VERSION
            technical["96"] += CANARY_ERROR_PROB
        if scenario == "low_volume" and minute >= LOW_VOLUME_FAIL_START:
            technical["96"] += LOW_VOLUME_FAIL_PROB
        if scenario == "normal_fluctuation" and FLUCTUATION_BURST[0] <= minute < FLUCTUATION_BURST[1]:
            technical["96"] += FLUCTUATION_BURST[2]

    code = _choose(rng, technical) or _choose(rng, declines) or "00"
    status = status_for(code)
    if status == "timeout":
        latency = CLIENT_TIMEOUT_MS + rng.randint(0, 800)
    elif code == "91":
        latency = _latency(rng, 180)
    else:
        latency = _latency(rng, median)

    tx_id = ids.tx()
    corr = ids.corr()
    event = {
        "transaction_id": tx_id, "timestamp": _iso(ts), "merchant_id": merchant, "bank_id": bank,
        "terminal_id": terminal, "payment_method": method, "transaction_type": tx_type,
        "transaction_status": status, "response_code": code, "processing_time_ms": latency,
        "service_name": service, "service_version": version, "channel": channel,
        "correlation_id": corr, "data_label": SYNTHETIC_LABEL,
    }
    events = [event]

    # İlişkili reversal kayıtları (aynı correlation_id)
    delay = None
    if status == "timeout" and channel == "POS" and rng.random() < TIMEOUT_REVERSAL_PROB:
        delay = rng.uniform(10, 40) + latency / 1000
    elif status == "approved" and tx_type == "sale":
        if rng.random() < auto_reversal_prob:
            delay = rng.uniform(3, 20) + latency / 1000
        elif rng.random() < VOID_PROB:
            delay = rng.uniform(120, 900)
    if delay is not None:
        events.append(_reversal(rng, ids, event, ts + timedelta(seconds=delay), corr))
    return events


def _reversal(rng: random.Random, ids: _Ids, original: dict, ts: datetime, corr: str) -> dict:
    return {
        **original, "transaction_id": ids.tx(), "timestamp": _iso(ts), "transaction_type": "reversal",
        "transaction_status": "approved", "response_code": "00", "processing_time_ms": _latency(rng, 200),
        "correlation_id": corr,
    }


def _orphan(rng: random.Random, ids: _Ids, ts: datetime, merchant: str | None = None) -> dict:
    merchant = merchant or rng.choice(POS_MERCHANTS)
    tx_id = ids.tx()
    return {
        "transaction_id": tx_id, "timestamp": _iso(ts), "merchant_id": merchant,
        "bank_id": rng.choices(BANKS, weights=BANK_WEIGHTS)[0], "terminal_id": f"T-{merchant[2:]}-{rng.randint(1, 4)}",
        "payment_method": "card_present", "transaction_type": "reversal", "transaction_status": "approved",
        "response_code": "00", "processing_time_ms": _latency(rng, 200), "service_name": "pos-gateway",
        "service_version": SERVICE_VERSION["pos-gateway"], "channel": "POS",
        "correlation_id": f"SYN-COR-ORPHAN-{ids.prefix}-{ids.n:07d}", "data_label": SYNTHETIC_LABEL,
    }


def _refund(rng: random.Random, ids: _Ids, ts: datetime) -> dict:
    merchant = rng.choice(POS_MERCHANTS + ECOM_MERCHANTS)
    pos = merchant in POS_MERCHANTS
    service = "pos-gateway" if pos else "payment-orchestrator"
    tx_id = ids.tx()
    return {
        "transaction_id": tx_id, "timestamp": _iso(ts), "merchant_id": merchant,
        "bank_id": rng.choices(BANKS, weights=BANK_WEIGHTS)[0],
        "terminal_id": f"T-{merchant[2:]}-{rng.randint(1, 4)}" if pos else f"VT-{merchant[2:]}",
        "payment_method": "card_present" if pos else "ecom_card", "transaction_type": "refund",
        "transaction_status": "approved", "response_code": "00", "processing_time_ms": _latency(rng, 400),
        "service_name": service, "service_version": SERVICE_VERSION[service], "channel": "POS" if pos else "ECOM",
        "correlation_id": ids.corr(), "data_label": SYNTHETIC_LABEL,
    }


def _generate_day(scenario: str, seed: int, today: bool) -> list[dict]:
    key = f"{seed}-{scenario}-{'D' if today else 'D-1'}"
    rng = random.Random(key)
    counts_rng = np.random.default_rng(zlib.crc32(key.encode()))
    ids = _Ids(f"{zlib.crc32(key.encode()) % 10000:04d}")
    day = DAY if today else DAY - timedelta(days=1)
    start = day.replace(hour=DAY_START_HOUR)
    end = day.replace(hour=DAY_END_HOUR)
    events: list[dict] = []
    for minute in range((DAY_END_HOUR - DAY_START_HOUR) * 60):
        rate = arrival_rate(minute, scenario, today)
        if today and scenario == "normal_fluctuation":
            rate *= counts_rng.gamma(8.0, 1 / 8.0)  # aşırı dağılım (overdispersion)
        minute_start = start + timedelta(minutes=minute)
        for _ in range(int(counts_rng.poisson(rate))):
            ts = minute_start + timedelta(milliseconds=rng.randint(0, 59_999))
            if rng.random() < REFUND_SHARE:
                events.append(_refund(rng, ids, ts))
            else:
                events.extend(_primary(rng, ids, scenario, today, minute, ts))
        orphan_rate = ORPHAN_PER_MIN
        if today and scenario == "reversal_anomaly" and minute >= REVERSAL_START:
            orphan_rate += REVERSAL_ORPHAN_PER_MIN
        if rng.random() < orphan_rate:
            merchant = REVERSAL_MERCHANT if orphan_rate > ORPHAN_PER_MIN else None
            events.append(_orphan(rng, ids, minute_start + timedelta(seconds=rng.uniform(0, 59)), merchant))
    end_iso = _iso(end)
    events = [e for e in events if e["timestamp"] < end_iso]
    if today and scenario == "data_quality_gap":
        events = _degrade(rng, events, start)
    return events


def _degrade(rng: random.Random, events: list[dict], day_start: datetime) -> list[dict]:
    """Log hattı sorunu: boşluk penceresindeki tüm olaylar kaybolur, sonrasında bazı alanlar boş gelir."""
    gap_start = _iso(day_start + timedelta(minutes=DQ_GAP[0]))
    gap_end = _iso(day_start + timedelta(minutes=DQ_GAP[1]))
    loss_start = _iso(day_start + timedelta(minutes=DQ_FIELD_LOSS_START))
    out = []
    for e in events:
        if gap_start <= e["timestamp"] < gap_end:
            continue  # olay toplama gecikmesi/kaybı (reversal ve orphan'lar dahil)
        if e["timestamp"] >= loss_start and rng.random() < DQ_FIELD_LOSS_SHARE:
            e = {**e, **{f: None for f in DQ_LOST_FIELDS}}
        out.append(e)
    return out


@lru_cache(maxsize=32)
def _cached(scenario: str, seed: int) -> tuple[dict, ...]:
    events = _generate_day(scenario, seed, today=False) + _generate_day(scenario, seed, today=True)
    events.sort(key=lambda e: (e["timestamp"], e["transaction_id"]))
    return tuple(events)


def generate_scenario(scenario: str, seed: int = SEED) -> list[dict]:
    """Senaryonun D-1 ve D olaylarını zaman sıralı döner (önbellekli; kopyası verilir)."""
    if scenario not in SCENARIOS:
        raise ValueError(f"Bilinmeyen senaryo: {scenario}. Geçerli: {', '.join(SCENARIOS)}")
    return [dict(e) for e in _cached(scenario, seed)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentetik ödeme senaryolarını üretir.")
    parser.add_argument("--export", action="store_true", help=f"JSONL olarak {GENERATED_DIR} altına yaz")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    for scenario in SCENARIOS:
        events = generate_scenario(scenario, args.seed)
        print(f"{scenario}: {len(events)} sentetik olay")
        if args.export:
            GENERATED_DIR.mkdir(parents=True, exist_ok=True)
            path = GENERATED_DIR / f"{scenario}.jsonl"
            with path.open("w", encoding="utf-8", newline="\n") as f:
                for e in events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            print(f"  → {path}")


if __name__ == "__main__":
    main()
