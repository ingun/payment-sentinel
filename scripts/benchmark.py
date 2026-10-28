"""
Sentetik veri kaynağı için küçük performans ölçümü.

Yöntem: numpy ile N kayıtlık 24 saatlik sentetik log üretilir. Her sorgu 1 kez ısındırılır,
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

from src.config import ROOT_DIR, SERVICES
from src.datasource import QueryFilter, SyntheticDataSource
from src.retriever import RunbookRetriever
from src.tools import PaymentToolkit
from src.validation import to_iso

REPEATS = 5
START = datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc)
CODES = np.array(["CARD_DECLINED", "INSUFFICIENT_FUNDS", "HTTP_500", "PROVIDER_TIMEOUT", "GATEWAY_504"], dtype=object)


def generate(n: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    offsets = np.sort(rng.integers(0, 24 * 3600, n))
    failed = rng.random(n) < 0.03
    codes = np.where(failed, CODES[rng.integers(0, len(CODES), n)], None)
    return pd.DataFrame({
        "timestamp": pd.to_datetime(START) + pd.to_timedelta(offsets, unit="s"),
        "transaction_id": [f"TX-BM-{i:08d}" for i in range(n)],
        "service": np.array(SERVICES, dtype=object)[rng.integers(0, len(SERVICES), n)],
        "status": np.where(failed, "failed", "success"),
        "error_code": codes,
        "latency_ms": rng.gamma(4.0, 60.0, n).astype(int) + 20,
        "payment_method": "card",
        "environment": "bench",
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
    cur = {"start": to_iso(end - timedelta(minutes=30)), "end": to_iso(end)}
    base = {"start": to_iso(end - timedelta(minutes=150)), "end": cur["start"]}
    f_cur = QueryFilter(end - timedelta(minutes=30), end)
    f_day = QueryFilter(START, end)
    retriever = RunbookRetriever()
    rows = []
    for n in sizes:
        df = generate(n)
        t = time.perf_counter()
        ds = SyntheticDataSource(df, label=f"bench-{n}")
        load_ms = round((time.perf_counter() - t) * 1000, 1)
        tk = PaymentToolkit(ds, retriever, incidents=[])
        rows.append({
            "Kayıt": f"{n:,}",
            "Yükleme+sıralama (ms)": load_ms,
            "summary 30 dk (ms)": measure(lambda: ds.summary(f_cur)),
            "histogram 24 sa / 5 dk (ms)": measure(lambda: ds.histogram(f_day, 5)),
            "group_by service 24 sa (ms)": measure(lambda: ds.group_by(f_day, "service")),
            "analyze_error_patterns 30 dk vs 120 dk (ms)": measure(lambda: tk.analyze_error_patterns(cur, base)),
        })
        print(rows[-1], flush=True)
    env = (f"Python {platform.python_version()} · pandas {pd.__version__} · numpy {np.__version__} · "
           f"{platform.system()} {platform.release()} · {platform.processor() or platform.machine()}")
    table = pd.DataFrame(rows).to_markdown(index=False) if _has_tabulate() else pd.DataFrame(rows).to_string(index=False)
    return (f"# Benchmark sonuçları\n\nÖlçüm zamanı: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n\nOrtam: {env}\n\n"
            f"Yöntem: 1 ısınma + {REPEATS} tekrar, `time.perf_counter` medyanı; veri bellekte, 24 saate yayılmış, %3 hata oranı.\n\n"
            f"```\n{table}\n```\n")


def _has_tabulate() -> bool:
    try:
        import tabulate  # noqa: F401
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+", default=[100_000, 1_000_000, 3_000_000])
    args = parser.parse_args()
    output = run(args.sizes)
    path = ROOT_DIR / "benchmarks" / "results.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(output, encoding="utf-8")
    print(output)
    print(f"Kaydedildi: {path}")
