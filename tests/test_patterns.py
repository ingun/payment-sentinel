from datetime import datetime, timedelta, timezone

from src.datasource import SyntheticDataSource
from src.retriever import RunbookRetriever
from src.tools import PaymentToolkit
from src.validation import to_iso
from tests.conftest import BASELINE, CURRENT, record


def detected(analysis: dict) -> dict:
    return {(p["type"], p["subject"]) for p in analysis["patterns"] if p["detected"]}


def test_error_spike_detects_rate_spike_and_code_concentration(make_toolkit):
    a = make_toolkit("error_spike").analyze_error_patterns(CURRENT, BASELINE)
    found = detected(a)
    assert ("ERROR_RATE_SPIKE", None) in found
    assert ("ERROR_CODE_CONCENTRATION", "PROVIDER_TIMEOUT") in found
    assert ("SERVICE_ERROR_SPIKE", "provider-connector") in found
    assert ("UNKNOWN_ERROR_CODE", "ERR_PRV_7781") in found
    assert a["affected_services"] == ["provider-connector"]
    assert a["onset_time"] == "2026-10-27T11:35:00Z"
    assert a["ongoing"] is True


def test_latency_degradation_detects_service_latency_and_cooccurrence(make_toolkit):
    a = make_toolkit("latency_degradation").analyze_error_patterns(CURRENT, BASELINE)
    found = detected(a)
    assert ("SERVICE_LATENCY_INCREASE", "acquirer-gateway") in found
    assert ("ERROR_LATENCY_COOCCURRENCE", None) in found
    assert "provider-connector" not in a["affected_services"]


def test_volume_drop_detected_without_error_spike(make_toolkit):
    a = make_toolkit("volume_drop").analyze_error_patterns(CURRENT, BASELINE)
    assert ("VOLUME_ANOMALY", "düşüş") in detected(a)
    assert "ERROR_RATE_SPIKE" not in a["detected_types"]


def test_no_false_alarms_on_normal_traffic_sliding_windows(make_toolkit):
    """Normal trafikte kayan 30 dk pencerelerin hiçbirinde patern tespit edilmemeli."""
    tk = make_toolkit("normal")
    start = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
    for step in range(0, 121, 10):
        cur_start = start + timedelta(minutes=step)
        cur = {"start": to_iso(cur_start), "end": to_iso(cur_start + timedelta(minutes=30))}
        base = {"start": to_iso(cur_start - timedelta(minutes=60)), "end": cur["start"]}
        a = tk.analyze_error_patterns(cur, base)
        assert a["detected_types"] == [], f"{cur['start']} penceresinde yanlış alarm: {a['detected_types']}"


def test_small_sample_is_not_flagged_as_spike():
    """3 işlemden 2'si başarısız olsa bile örneklem yetersiz olduğu için alarm üretilmez."""
    base_records = [record(f"2026-10-27T10:{m:02d}:00Z", i=m) for m in range(0, 59)]
    cur_records = [record("2026-10-27T11:00:10Z", "failed", error_code="HTTP_500", i=100),
                   record("2026-10-27T11:00:20Z", "failed", error_code="HTTP_500", i=101),
                   record("2026-10-27T11:00:30Z", i=102)]
    tk = PaymentToolkit(SyntheticDataSource.from_records(base_records + cur_records), RunbookRetriever(), incidents=[])
    a = tk.analyze_error_patterns({"start": "2026-10-27T11:00:00Z", "end": "2026-10-27T11:05:00Z"},
                                  {"start": "2026-10-27T10:00:00Z", "end": "2026-10-27T11:00:00Z"})
    spike = next(p for p in a["patterns"] if p["type"] == "ERROR_RATE_SPIKE")
    assert spike["detected"] is False and spike["insufficient_data"] is True
    assert "ERROR_CODE_CONCENTRATION" not in a["detected_types"]


def test_every_pattern_explains_its_rule(make_toolkit):
    a = make_toolkit("error_spike").analyze_error_patterns(CURRENT, BASELINE)
    for p in a["patterns"]:
        assert p["rule"] and "sample_size" in p
    assert "demo" in a["threshold_note"]
