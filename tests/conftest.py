import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_generator import SCENARIOS  # noqa: E402
from src.repositories import SyntheticPaymentRepository  # noqa: E402
from src.retriever import RunbookRetriever  # noqa: E402
from src.tools import PaymentToolkit  # noqa: E402

CURRENT = {"start": "2026-10-27T11:00:00Z", "end": "2026-10-27T12:00:00Z"}
BASELINE = {"start": "2026-10-27T09:00:00Z", "end": "2026-10-27T11:00:00Z"}


@pytest.fixture(scope="session")
def retriever() -> RunbookRetriever:
    return RunbookRetriever()


@pytest.fixture(scope="session")
def repos() -> dict[str, SyntheticPaymentRepository]:
    # Veri bellekte, sabit seed ile üretilir; dosyaya bağımlı değildir
    return {s: SyntheticPaymentRepository.from_scenario(s) for s in SCENARIOS}


@pytest.fixture
def make_toolkit(repos, retriever):
    def _make(scenario: str = "bank_timeout", faults: dict | None = None, incidents=None) -> PaymentToolkit:
        return PaymentToolkit(repos[scenario], retriever, incidents=incidents, faults=faults)
    return _make


def pair(filters: dict | None = None, **extra) -> dict:
    return {"current_window": CURRENT, "baseline_window": BASELINE, "filters": filters or {}, **extra}


def detected_names(detections: list[dict]) -> set[tuple[str, str | None]]:
    return {(d["detector"], d["subject"]) for d in detections if d["detected"]}
