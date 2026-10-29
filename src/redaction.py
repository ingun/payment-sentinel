"""
Hassas alan maskeleme. Örnek kayıtlar, raporlar ve loglar bu modülden geçer.

Sentetik veride kart numarası yoktur; yine de PAN benzeri rakam dizileri ve e-posta
adresleri her metinde maskelenir. Örnek kayıtlarda yalnızca izinli alanlar döner.
"""

import logging
import re

from src.domain import EVENT_FIELDS

# Kimlik ve zaman damgalarının parçası olan rakam dizileri (ör. SYN-TX-...-0006258) PAN sayılmaz
PAN_RE = re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
SAMPLE_FIELDS = [f for f in EVENT_FIELDS if f != "data_label"]


def _mask_pan(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return f"****{digits[-4:]}"


def redact_text(text: str) -> str:
    return EMAIL_RE.sub("[e-posta maskelendi]", PAN_RE.sub(_mask_pan, text))


def mask_terminal(terminal_id: str | None) -> str | None:
    """Terminal kimliğinin yalnızca son 2 karakteri görünür: T-007-3 → T-****-3 benzeri."""
    if not terminal_id:
        return terminal_id
    keep = terminal_id[-2:]
    prefix = terminal_id.split("-", 1)[0]
    return f"{prefix}-***{keep}"


def redact_record(record: dict) -> dict:
    out = {}
    for field in SAMPLE_FIELDS:
        value = record.get(field)
        if field == "terminal_id":
            value = mask_terminal(value)
        elif isinstance(value, str):
            value = redact_text(value)
        out[field] = value
    return out


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.msg = record.getMessage()
            record.args = None
        record.msg = redact_text(str(record.msg))
        return True
