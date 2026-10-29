"""
Yerel dosya tabanlı bilgi depoları: geçmiş incident kayıtları (JSON) ve runbook'lar (Markdown).
İçerikler güvenilmeyen veri kabul edilir; talimat olarak yorumlanmaz.
"""

import json
from pathlib import Path

from src.config import INCIDENTS_PATH, RUNBOOK_DIR
from src.repositories.base import HistoricalIncidentRepository, RunbookRepository


class JsonIncidentRepository(HistoricalIncidentRepository):
    def __init__(self, path: Path = INCIDENTS_PATH):
        self.path = path

    def all(self) -> list[dict]:
        with self.path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"{self.path.name}: incident listesi bekleniyordu")
        return data


class InMemoryIncidentRepository(HistoricalIncidentRepository):
    def __init__(self, incidents: list[dict]):
        self.incidents = list(incidents)

    def all(self) -> list[dict]:
        return list(self.incidents)


class MarkdownRunbookRepository(RunbookRepository):
    def __init__(self, directory: Path = RUNBOOK_DIR):
        self.directory = directory

    def documents(self) -> list[tuple[str, str]]:
        return [(p.name, p.read_text(encoding="utf-8")) for p in sorted(self.directory.glob("*.md"))]
