import numpy as np
import pandas as pd
import pytest

from src.data_generator import generate_scenario
from src.metrics import compare_metrics, excess_failures, metrics_from_summary, pct_change, two_proportion_z
from src.validation import safe_rate
from tests.conftest import BASELINE, CURRENT


def test_normal_period_metrics_match_independent_calculation(make_toolkit):
    tk = make_toolkit("normal")
    result = tk.calculate_payment_metrics(CURRENT["start"], CURRENT["end"])

    df = pd.DataFrame(generate_scenario("normal"))
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    window = df[(df["timestamp"] >= CURRENT["start"]) & (df["timestamp"] < CURRENT["end"])]
    failed = int((window["status"] == "failed").sum())

    assert result["total_attempts"] == len(window)
    assert result["failed"] == failed
    assert result["successful"] == len(window) - failed
    assert result["error_rate"] == round(failed / len(window), 4)
    assert result["avg_latency_ms"] == round(float(window["latency_ms"].mean()), 1)
    assert result["p95_latency_ms"] == round(float(np.percentile(window["latency_ms"], 95)), 1)
    assert result["sufficient_data"] is True


def test_safe_rate_and_pct_change_handle_zero_denominator():
    assert safe_rate(5, 0) is None
    assert safe_rate(0, 10) == 0.0
    assert pct_change(10, 0) is None
    assert pct_change(None, 5) is None
    assert pct_change(15, 10) == 50.0


def test_empty_window_metrics_are_safe():
    m = metrics_from_summary({"total": 0, "failed": 0, "avg_latency_ms": None, "p95_latency_ms": None})
    assert m["error_rate"] is None and m["sufficient_data"] is False
    cmp = compare_metrics(m, m, 30, 120)
    assert cmp["error_rate_delta_pp"] is None and cmp["volume_change_pct"] is None and cmp["excess_failed"] == 0


def test_error_rate_increase_is_measured(make_toolkit):
    cmp = make_toolkit("error_spike").compare_time_windows(CURRENT, BASELINE)
    assert cmp["error_rate_delta_pp"] > 5
    assert cmp["error_rate_ratio"] > 3
    assert cmp["excess_failed"] > 25


def test_excess_failures_and_z_statistic():
    cur = {"error_rate": 0.1, "total_attempts": 100, "failed": 10}
    base = {"error_rate": 0.02, "total_attempts": 400, "failed": 8}
    assert excess_failures(cur, base) == 8
    assert two_proportion_z(10, 100, 8, 400) == pytest.approx(3.84, abs=0.02)
    assert two_proportion_z(0, 0, 1, 10) is None
