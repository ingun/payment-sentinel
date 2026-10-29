"""
Veri erişim katmanı. Araçlar yalnızca bu arayüzleri kullanır; ham olay kümesi hiçbir zaman
LLM'e gönderilmez, yalnızca toplulaştırılmış sonuçlar ve sınırlı, maskelenmiş örnek kayıtlar döner.
"""

from src.repositories.base import (
    DataSourceError,
    DataSourceUnavailable,
    HistoricalIncidentRepository,
    MalformedResponseError,
    PaymentEventRepository,
    QueryFilter,
    QueryLimitError,
    RunbookRepository,
)
from src.repositories.knowledge import InMemoryIncidentRepository, JsonIncidentRepository, MarkdownRunbookRepository
from src.repositories.synthetic import SyntheticPaymentRepository

__all__ = [
    "DataSourceError", "DataSourceUnavailable", "HistoricalIncidentRepository", "InMemoryIncidentRepository",
    "JsonIncidentRepository", "MalformedResponseError", "MarkdownRunbookRepository", "PaymentEventRepository",
    "QueryFilter", "QueryLimitError", "RunbookRepository", "SyntheticPaymentRepository", "create_repository",
]


def create_repository(kind: str, scenario: str = "bank_timeout") -> PaymentEventRepository:
    if kind == "elasticsearch":
        from src.repositories.elasticsearch import ElasticsearchPaymentRepository

        return ElasticsearchPaymentRepository.from_config()
    return SyntheticPaymentRepository.from_scenario(scenario)
