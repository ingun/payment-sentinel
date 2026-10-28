import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_generator import generate_scenario  # noqa: E402
from src.datasource import SyntheticDataSource  # noqa: E402
from src.retriever import RunbookRetriever  # noqa: E402
from src.tools import PaymentToolkit  # noqa: E402

CURRENT = {"start": "2026-10-27T11:30:00Z", "end": "2026-10-27T12:00:00Z"}
BASELINE = {"start": "2026-10-27T09:30:00Z", "end": "2026-10-27T11:30:00Z"}
REQUEST = "Son 30 dakikadaki ödeme başarısızlıklarını araştır, normal dönemle karşılaştır ve olası incident nedenlerini raporla."


@pytest.fixture(scope="session")
def retriever() -> RunbookRetriever:
    return RunbookRetriever()


@pytest.fixture(scope="session")
def datasources() -> dict[str, SyntheticDataSource]:
    # Dosyaya bağımlı olmamak için veri bellekte, aynı seed ile üretilir
    return {s: SyntheticDataSource.from_records(generate_scenario(s), label=s)
            for s in ("normal", "error_spike", "latency_degradation", "volume_drop")}


@pytest.fixture
def make_toolkit(datasources, retriever):
    def _make(scenario: str = "error_spike", faults: dict | None = None) -> PaymentToolkit:
        return PaymentToolkit(datasources[scenario], retriever, faults=faults)
    return _make


def record(ts: str, status: str = "success", service: str = "api-gateway", error_code: str | None = None, latency: int = 100, i: int = 0) -> dict:
    return {"timestamp": ts, "transaction_id": f"TX-T-{i:06d}", "service": service, "status": status,
            "error_code": error_code, "latency_ms": latency, "payment_method": "card", "environment": "test"}
