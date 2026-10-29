"""
Sentetik (bellek içi) depo için küçük performans ölçümü.

Yöntem: numpy ile N olaylık 24 saatlik sentetik ödeme verisi üretilir. Her sorgu 1 kez ısındırılır,
ardından REPEATS kez çalıştırılır ve time.perf_counter ile ölçülen sürelerin medyanı raporlanır.
Elasticsearch ölçülmez (canlı küme yok).

Çalıştırma: python -m scripts.benchmark [--sizes 100000 1000000]
"""

import argparse
import platform
import statistics
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from src.config import ROOT_DIR
from src.domain import BANKS, MERCHANTS, SERVICES
from src.repositories import QueryFilter, SyntheticPaymentRepository
from src.retriever import RunbookRetriever
from src.tools import PaymentToolkit
from src.validation import to_iso

REPEATS = 5
START = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)
CODES = np.array(["00"] * 90 + ["05"] * 4 + ["51"] * 3 + ["96", "91", "68"], dtype=object)
STATUS = {"00": "approved", "05": "declined", "51": "declined", "96": "error", "91": "error", "68": "timeout"}


def generate(n: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    offsets = np.sort(rng.integers(0, 24 * 3600 * 1000, n))
    codes = CODES[rng.integers(0, len(CODES), n)]
    types = np.where(rng.random(n) < 0.01, "reversal", np.where(rng.random(n) < 0.7, "sale", "authorization"))
    ids = np.char.add("SYN-BM-", np.arange(n).astype(str))
    return pd.DataFrame({
        "transaction_id": ids, "timestamp": pd.to_datetime(START) + pd.to_timedelta(offsets, unit="ms"),
        "merchant_id": np.array(MERCHANTS, dtype=object)[rng.integers(0, len(MERCHANTS), n)],
        "bank_id": np.array(BANKS, dtype=object)[rng.integers(0, len(BANKS), n)], "terminal_id": "T-001-1",
        "payment_method": "card_present", "transaction_type": types, "transaction_status": [STATUS[c] for c in codes],
        "response_code": codes, "processing_time_ms": rng.gamma(4.0, 90.0, n).astype(int) + 30,
        "service_name": np.array(SERVICES, dtype=object)[rng.integers(0, len(SERVICES), n)], "service_version": "1.0.0",
        "channel": "POS", "correlation_id": ids, "data_label": "synthetic",
    })


def measure(fn) -> float:
    fn()  # ısınma
    times = []
    for _ in range(REPEATS):
        t = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t) * 1000)
    return round(statistics.median(times), 2)


def run(sizes: list[int]) -> str:
    end = START + timedelta(hours=24)
    cur = {"start": to_iso(end - timedelta(minutes=60)), "end": to_iso(end)}
    base = {"start": to_iso(end - timedelta(minutes=180)), "end": cur["start"]}
    f_cur = QueryFilter(end - timedelta(minutes=60), end)
    f_day = QueryFilter(START, end)
    retriever = RunbookRetriever()
    rows = []
    for n in sizes:
        df = generate(n)
        t = time.perf_counter()
        repo = SyntheticPaymentRepository(df, label=f"bench-{n}")
        load_ms = round((time.perf_counter() - t) * 1000, 1)
        tk = PaymentToolkit(repo, retriever, incidents=[])
        rows.append({
            "Olay": f"{n:,}",
            "Yükleme+sıralama (ms)": load_ms,
            "summary 60 dk (ms)": measure(lambda: repo.summary(f_cur)),
            "histogram 24 sa / 5 dk (ms)": measure(lambda: repo.histogram(f_day, 5)),
            "group_by bank 24 sa (ms)": measure(lambda: repo.group_by(f_day, "bank_id")),
            "reversal_linkage 60 dk (ms)": measure(lambda: repo.reversal_linkage(f_cur, 30, 60)),
            "compare_with_baseline 60 vs 120 dk (ms)": measure(lambda: tk.compare_with_baseline(cur, base, {})),
        })
        print(rows[-1], flush=True)
    env = (f"Python {platform.python_version()} · pandas {pd.__version__} · numpy {np.__version__} · "
           f"{platform.system()} {platform.release()} · {platform.processor() or platform.machine()}")
    headers = list(rows[0])
    table = "\n".join(["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
                      + ["| " + " | ".join(str(r[h]) for h in headers) + " |" for r in rows])
    return (f"# Benchmark sonuçları (sentetik bellek içi depo)\n\nÖlçüm zamanı: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n\n"
            f"Ortam: {env}\n\nYöntem: 1 ısınma + {REPEATS} tekrar, `time.perf_counter` medyanı; veri bellekte, 24 saate yayılmış. "
            "Elasticsearch ölçülmedi.\n\n" + table + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+", default=[100_000, 1_000_000])
    args = parser.parse_args()
    output = run(args.sizes)
    path = ROOT_DIR / "benchmarks" / "results.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(output, encoding="utf-8")
    print(output)
    print(f"Kaydedildi: {path}")
