"""Deterministik dedektör testleri: her senaryoda beklenen tespitler ve yanlış alarm olmaması."""

from datetime import timedelta

import pytest

from src.analytics import detectors as D
from src.analytics.thresholds import DetectionThresholds, Thresholds, load_thresholds
from src.metrics import bonferroni_z, excess_count, spearman, two_proportion_z
from src.tools import detections_of
from src.validation import parse_timestamp, to_iso
from tests.conftest import BASELINE, CURRENT, detected_names, pair

TH = DetectionThresholds()
REQUIRED_FIELDS = ["metric", "observed", "baseline", "abs_deviation", "rel_deviation_pct", "window", "scope",
                   "sample_size", "evidence_quality", "method", "limitations"]


def baseline_detections(tk, filters=None, current=CURRENT):
    return tk.compare_with_baseline(current, BASELINE, filters or {})["detections"]


def test_detection_contains_all_required_fields(make_toolkit):
    det = next(d for d in baseline_detections(make_toolkit("bank_timeout")) if d["detector"] == "TECHNICAL_FAILURE_SPIKE")
    for field in REQUIRED_FIELDS:
        assert field in det
    assert det["window"] == CURRENT and det["baseline_window"] == BASELINE
    assert det["sample_size"]["current"] > 0 and det["abs_deviation"] > 0
    assert det["method"].startswith("İki oran z-testi")


def test_technical_failure_spike_and_bank_isolation(make_toolkit):
    tk = make_toolkit("bank_timeout")
    assert {"TECHNICAL_FAILURE_SPIKE", "TIMEOUT_RATE_SPIKE"} <= {d for d, _ in detected_names(baseline_detections(tk))}
    out = tk.breakdown_by_dimension(CURRENT, BASELINE, "bank_id", {})
    assert out["isolated"] and out["isolated_segment"] == "BANK_B"
    assert out["excess_share"] >= TH.segment_min_excess_share
    assert out["segments"][0]["rate"] > out["segments"][0]["rest_rate"]


def test_response_code_concentration_isolated_to_bank(make_toolkit):
    out = make_toolkit("response_code_spike").analyze_response_codes(**pair(dimension="bank_id"))
    assert ("RESPONSE_CODE_CONCENTRATION", "05") in detected_names(out["detections"])
    assert out["isolation"]["isolated_segment"] == "BANK_C"
    assert next(c for c in out["codes"] if c["code"] == "05")["status"] == "declined"


def test_decline_spike_is_not_reported_as_technical_failure(make_toolkit):
    names = {d for d, _ in detected_names(baseline_detections(make_toolkit("response_code_spike")))}
    assert "DECLINE_RATE_SPIKE" in names and "TECHNICAL_FAILURE_SPIKE" not in names


def test_latency_precedes_timeouts_but_is_reported_as_correlation(make_toolkit):
    out = make_toolkit("latency_then_failures").analyze_latency_timeouts(**pair({"service_name": "pos-gateway"}))
    corr = out["detections"][0]
    assert corr["detector"] == "LATENCY_TIMEOUT_CORRELATION" and corr["detected"]
    assert out["lead_minutes"] > 0 and out["rho"] >= TH.correlation_min_rho
    assert corr["claim_type"] == "correlation"
    assert any("nedensellik" in x for x in corr["limitations"])


def test_bank_timeout_latency_and_failures_start_together(make_toolkit):
    out = make_toolkit("bank_timeout").analyze_latency_timeouts(**pair({"bank_id": "BANK_B"}))
    assert out["detections"][0]["detected"] and out["lead_minutes"] == 0


def test_reversal_anomaly_counts_only_unexplained_technical_reversals(make_toolkit):
    out = make_toolkit("reversal_anomaly").analyze_reversals(**pair())
    det = out["detection"]
    assert det["detected"] and det["extra"]["technical"] >= TH.reversal_min_technical
    assert out["reconciliation_risk"]["level"] in ("MEDIUM", "HIGH")
    # Timeout'ların koruyucu reversal'ları anomali sayılmaz
    timeout_case = make_toolkit("bank_timeout").analyze_reversals(**pair())
    assert not timeout_case["detection"]["detected"]
    assert timeout_case["current"]["categories"]["after_failure"] > 10


def test_service_version_comparison_is_a_hypothesis(make_toolkit):
    out = make_toolkit("version_regression").compare_service_versions(CURRENT, "payment-orchestrator", {})
    det = out["detection"]
    assert det["detected"] and det["subject"] == "2.4.0" and det["claim_type"] == "hypothesis"
    new = next(d for d in out["deployments"] if d["version"] == "2.4.0")
    assert new["new_in_lookback"] and new["first_seen"].startswith("2026-10-27T10:4")
    single = make_toolkit("bank_timeout").compare_service_versions(CURRENT, "pos-gateway", {})
    assert not single["detection"]["detected"] and single["detection"]["insufficient_data"]


def test_volume_uses_same_window_yesterday(make_toolkit):
    det = next(d for d in baseline_detections(make_toolkit("normal_baseline")) if d["detector"] == "VOLUME_ANOMALY")
    assert det["extra"]["reference"] == "day_over_day" and not det["detected"]


def test_volume_drop_detected_on_constructed_buckets():
    ctx = D.Ctx(window=CURRENT, baseline_window=BASELINE)
    ref = [{"total": 100 + (i % 3)} for i in range(12)]
    cur = [{"total": 40 + (i % 2)} for i in range(12)]
    det = D.volume_anomaly(cur, ref, ctx, TH, "day_over_day")
    assert det["detected"] and det["subject"] == "düşüş" and det["rel_deviation_pct"] < -50
    trailing = D.volume_anomaly(cur, ref, ctx, TH, "trailing")
    assert any("sezonsallık" in x for x in trailing["limitations"])


@pytest.mark.parametrize("scenario", ["normal_baseline", "normal_fluctuation"])
def test_no_false_alarms_on_rolling_windows(make_toolkit, scenario):
    """Normal senaryolarda kayan 60 dk pencerelerin hiçbirinde tespit üretilmez."""
    tk = make_toolkit(scenario)
    start = parse_timestamp("2026-10-27T08:30:00Z")
    while start + timedelta(minutes=60) <= parse_timestamp("2026-10-27T12:00:00Z"):
        cw = {"start": to_iso(start), "end": to_iso(start + timedelta(minutes=60))}
        bw = {"start": to_iso(start - timedelta(minutes=120)), "end": cw["start"]}
        out = tk.compare_with_baseline(cw, bw, {})
        assert not detected_names(out["detections"]), (scenario, cw)
        start += timedelta(minutes=15)
    for tool, args in [("breakdown_by_dimension", pair(dimension="bank_id")), ("analyze_response_codes", pair(dimension="bank_id")),
                       ("analyze_reversals", pair())]:
        assert not detected_names(detections_of(tool, tk.call(tool, args))), tool


def test_low_volume_is_insufficient_not_anomalous(make_toolkit):
    cw = {"start": "2026-10-27T11:28:00Z", "end": "2026-10-27T11:58:00Z"}
    dets = baseline_detections(make_toolkit("low_volume"), current=cw)
    tech = next(d for d in dets if d["detector"] == "TECHNICAL_FAILURE_SPIKE")
    assert tech["observed"] > tech["baseline"]  # oran yüksek görünüyor...
    assert not tech["detected"] and tech["evidence_quality"] == "insufficient"  # ...ama iddia edilmiyor
    assert not detected_names(dets)


def test_statistics_helpers():
    assert two_proportion_z(50, 1000, 10, 1000) > 5
    assert two_proportion_z(0, 0, 1, 10) is None
    assert excess_count(30, 1000, 10, 1000) == 20
    assert bonferroni_z(0.001, 5) > bonferroni_z(0.001, 1)
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert spearman([1, 1, 1], [1, 2, 3]) is None


def test_thresholds_are_configurable(tmp_path):
    path = tmp_path / "t.toml"
    path.write_text("[detection]\nmin_z = 5.0\n[risk]\nhigh_threshold = 80\n", encoding="utf-8")
    th = load_thresholds(path)
    assert th.detection.min_z == 5.0 and th.risk.high_threshold == 80
    assert th.detection.technical_min_delta_pp == Thresholds().detection.technical_min_delta_pp
    assert load_thresholds(tmp_path / "missing.toml") == Thresholds()
