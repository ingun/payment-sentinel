"""
Girdi, araç çıktısı ve rapor doğrulamaları.
"""

import re
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from src.config import MAX_WINDOW_HOURS


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
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_rate(numerator: int | float, denominator: int | float) -> float | None:
    """Payda sıfırsa None döner; sıfıra bölme hatası oluşmaz."""
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def validate_tool_output(tool_name: str, output: dict) -> list[str]:
    from src.schemas import TOOL_OUTPUT_SCHEMAS

    schema = TOOL_OUTPUT_SCHEMAS.get(tool_name)
    if schema is None:
        return [f"'{tool_name}' için şema tanımlı değil"]
    try:
        schema.model_validate(output)
    except ValidationError as exc:
        return [f"{tool_name} çıktısı şemaya uymuyor: {err['loc']} {err['msg']}" for err in exc.errors()]
    return []


# ---------- Sonuç arama ----------


def find_result(results: list[dict], tool: str, where: dict | None = None) -> dict | None:
    """Verilen araç için (ve argümanları `where` ile eşleşen) son başarılı çıktıyı döner."""
    for item in reversed(results):
        if item["tool"] != tool or item.get("status") != "ok":
            continue
        args = item.get("args") or {}
        if where and any(args.get(k) != v for k, v in where.items()):
            continue
        return item["output"]
    return None


def find_all(results: list[dict], tool: str) -> list[dict]:
    return [r["output"] for r in results if r["tool"] == tool and r.get("status") == "ok"]


def find_baseline(results: list[dict], scope: dict) -> dict | None:
    """Kapsamdaki pencerelerle eşleşen son compare_with_baseline çıktısı (LLM'in pencereyi değiştirmesine karşı)."""
    for output in reversed(find_all(results, "compare_with_baseline")):
        try:
            same = (parse_timestamp(output["window"]["start"]) == parse_timestamp(scope["current_start"])
                    and parse_timestamp(output["window"]["end"]) == parse_timestamp(scope["current_end"])
                    and parse_timestamp(output["baseline_window"]["start"]) == parse_timestamp(scope["baseline_start"]))
        except (KeyError, InputValidationError):
            continue
        if same and output.get("filters", {}) == scope.get("filters", {}):
            return output
    return None


def find_coverage(results: list[dict], scope: dict) -> dict | None:
    """Kapsam filtreleriyle eşleşen son check_data_coverage çıktısı (takip turlarındaki dar kapsamlar hariç)."""
    for output in reversed(find_all(results, "check_data_coverage")):
        if output.get("filters", {}) == scope.get("filters", {}):
            return output
    return None


# ---------- Sayı doğrulama ----------

_NUMBER_RE = re.compile(r"(?<![\w:./-])(\d+(?:[.,]\d+)?)(?![\w:/-])")


def collect_allowed_numbers(results: list[dict], extra: list[float] | None = None) -> set[float]:
    """Araç çıktılarındaki tüm sayılar ve oranların yüzde karşılıkları."""
    allowed: set[float] = set(extra or [])

    def walk(value: object) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            allowed.add(float(value))
            if isinstance(value, float) and abs(value) <= 1:
                allowed.add(round(value * 100, 2))
                allowed.add(round(value * 100, 1))
        elif isinstance(value, str):
            # Araçların kendi ürettiği kanıt metinlerindeki sayılar da deterministiktir
            allowed.update(float(n.replace(",", ".")) for n in _NUMBER_RE.findall(value))
        elif isinstance(value, dict):
            for v in value.values():
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
        # Regex işaretsiz yakalar; "-37,5" değeri metinde "%37,5 düşüş" olarak yazılır, mutlak değerle karşılaştırılır
        if not any(abs(number - abs(a)) <= max(0.06, abs(a) * 0.01) for a in allowed):
            unverified.append(match)
    return unverified


def scope_numbers(scope: dict) -> list[float]:
    nums = [float(scope.get("window_minutes", 0)), float(scope.get("baseline_minutes", 0))]
    for key in ("current_start", "current_end", "baseline_start", "baseline_end"):
        if scope.get(key):
            nums += [float(x) for x in re.findall(r"\d+", scope[key])]
    return nums
