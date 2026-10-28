"""
Girdi, araç çıktısı ve rapor doğrulamaları.
"""

import re
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from src.config import MAX_WINDOW_HOURS, MIN_SAMPLES
from src.schemas import TOOL_OUTPUT_SCHEMAS, IncidentReport


class InputValidationError(ValueError):
    """Kullanıcı veya agent'tan gelen hatalı girdi."""


def parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise InputValidationError(f"Geçersiz zaman formatı: '{value}'. ISO-8601 bekleniyor.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_window(start: str | datetime, end: str | datetime) -> tuple[datetime, datetime]:
    start_dt, end_dt = parse_timestamp(start), parse_timestamp(end)
    if start_dt >= end_dt:
        raise InputValidationError(f"Başlangıç ({start_dt:%H:%M}) bitişten ({end_dt:%H:%M}) önce olmalı.")
    if end_dt - start_dt > timedelta(hours=MAX_WINDOW_HOURS):
        raise InputValidationError(f"Zaman aralığı en fazla {MAX_WINDOW_HOURS} saat olabilir.")
    return start_dt, end_dt


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_rate(numerator: int | float, denominator: int | float) -> float | None:
    """Payda sıfırsa None döner; sıfıra bölme hatası oluşmaz."""
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def has_sufficient_samples(count: int, minimum: int = MIN_SAMPLES) -> bool:
    return count >= minimum


def validate_tool_output(tool_name: str, output: dict) -> list[str]:
    schema = TOOL_OUTPUT_SCHEMAS.get(tool_name)
    if schema is None:
        return [f"'{tool_name}' için şema tanımlı değil"]
    try:
        schema.model_validate(output)
    except ValidationError as exc:
        return [f"{tool_name} çıktısı şemaya uymuyor: {err['loc']} {err['msg']}" for err in exc.errors()]
    return []


# ---------- Kanıt ve rapor tutarlılığı ----------


def check_required_evidence(results: list[dict], scope: dict) -> list[str]:
    """Raporlamadan önce bulunması gereken araç sonuçlarını kontrol eder; eksik olanları döner."""
    missing = []
    if find_result(results, "compare_time_windows", scope) is None:
        missing.append("compare_time_windows")
    analysis = find_result(results, "analyze_error_patterns", scope)
    if analysis is None:
        missing.append("analyze_error_patterns")
    elif analysis["detected_types"]:
        # Şüpheli patern varsa geçmiş olay ve runbook araması da yapılmış olmalı
        for tool in ("search_similar_incidents", "search_runbooks"):
            if find_result(results, tool, scope) is None:
                missing.append(tool)
    return missing


def find_result(results: list[dict], tool: str, scope: dict) -> dict | None:
    """Kapsamdaki pencere ve servisle eşleşen son başarılı araç çıktısını bulur."""
    for item in reversed(results):
        if item["tool"] != tool or item["status"] != "ok":
            continue
        output = item["output"]
        if tool == "compare_time_windows":
            cur, base = output["current"], output["baseline"]
            if (
                _same_window(cur, scope["current_start"], scope["current_end"])
                and _same_window(base, scope["baseline_start"], scope["baseline_end"])
                and cur.get("service") == scope.get("service")
            ):
                return output
        elif tool == "analyze_error_patterns":
            if (
                _same_window(output, scope["current_start"], scope["current_end"])
                and _same_window(
                    {"window_start": output["baseline_start"], "window_end": output["baseline_end"]},
                    scope["baseline_start"],
                    scope["baseline_end"],
                )
                and output.get("service") == scope.get("service")
            ):
                return output
        elif tool == "calculate_payment_metrics":
            if _same_window(output, scope["current_start"], scope["current_end"]) and output.get("service") == scope.get("service"):
                return output
        else:
            return output
    return None


def _same_window(output: dict, start: str, end: str) -> bool:
    try:
        return parse_timestamp(output["window_start"]) == parse_timestamp(start) and parse_timestamp(
            output["window_end"]
        ) == parse_timestamp(end)
    except (KeyError, InputValidationError):
        return False


_NUMBER_RE = re.compile(r"(?<![\w:./-])(\d+(?:[.,]\d+)?)(?![\w:/-])")


def collect_allowed_numbers(results: list[dict], extra: list[float] | None = None) -> set[float]:
    """Araç çıktılarındaki tüm sayılar ve bunların yüzde karşılıkları."""
    allowed: set[float] = set(extra or [])

    def walk(value: object) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            allowed.add(float(value))
            if isinstance(value, float) and abs(value) <= 1:
                allowed.add(round(value * 100, 2))
                allowed.add(round(value * 100, 1))
        elif isinstance(value, dict):
            for k, v in value.items():
                # Araçların kendi ürettiği kanıt metinlerindeki sayılar da deterministiktir
                if k == "evidence" and isinstance(v, str):
                    allowed.update(float(n.replace(",", ".")) for n in _NUMBER_RE.findall(v))
                else:
                    walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    for item in results:
        if item.get("status") == "ok":
            walk(item.get("output"))
    return allowed


def find_unverified_numbers(text: str, allowed: set[float]) -> list[str]:
    """Metindeki, araç çıktılarıyla desteklenmeyen sayıları döner."""
    unverified = []
    for match in _NUMBER_RE.findall(text):
        number = float(match.replace(",", "."))
        # Regex işaretsiz yakalar; "-%37,5" değeri metinde "%37,5 düşüş" olarak yazılır, mutlak değerle karşılaştırılır
        if not any(abs(number - abs(a)) <= max(0.06, abs(a) * 0.01) for a in allowed):
            unverified.append(match)
    return unverified


def check_report_consistency(report: IncidentReport, results: list[dict]) -> list[str]:
    """Rapordaki sayıların araç çıktılarıyla birebir aynı olduğunu doğrular."""
    issues = []
    scope = report.scope
    comparison = find_result(results, "compare_time_windows", scope)
    if comparison and report.comparison:
        if report.comparison.model_dump() != comparison:
            issues.append("Rapordaki karşılaştırma değerleri araç çıktısıyla uyuşmuyor")
        if report.current and report.current.model_dump() != comparison["current"]:
            issues.append("Rapordaki güncel dönem metrikleri araç çıktısıyla uyuşmuyor")
    allowed = collect_allowed_numbers(results, extra=_scope_numbers(scope))
    for obs in report.observations:
        bad = find_unverified_numbers(obs.text, allowed)
        if bad:
            issues.append(f"{obs.id} gözleminde doğrulanmayan sayı: {', '.join(bad)}")
    return issues


def check_recommendations_supported(report: IncidentReport) -> list[str]:
    """Her öneri bir runbook kaynağına veya geçerli bir gözleme dayanmalı."""
    observation_ids = {o.id for o in report.observations}
    issues = []
    for rec in report.recommended_steps:
        has_source = bool(rec.source) and rec.source != "-"
        has_ref = any(ref in observation_ids for ref in rec.evidence_refs)
        if not (has_source or has_ref):
            issues.append(f"Desteklenmeyen öneri: {rec.text[:60]}")
    for exp in report.possible_explanations:
        if not any(ref in observation_ids for ref in exp.evidence_refs):
            issues.append(f"Kanıtsız açıklama: {exp.text[:60]}")
    return issues


def _scope_numbers(scope: dict) -> list[float]:
    return [float(scope.get("window_minutes", 0)), float(scope.get("baseline_minutes", 0))]
