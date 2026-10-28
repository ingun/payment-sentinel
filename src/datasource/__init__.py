"""
Veri erişim katmanı. Araçlar yalnızca DataSource arayüzünü kullanır; ham log
kümesi hiçbir zaman LLM'e gönderilmez, yalnızca toplulaştırılmış sonuçlar ve
sınırlı sayıda örnek kayıt döner.
"""

from src.datasource.base import DataSource, QueryFilter, QueryLimitError
from src.datasource.synthetic import SyntheticDataSource

__all__ = ["DataSource", "QueryFilter", "QueryLimitError", "SyntheticDataSource", "create_datasource"]


def create_datasource(kind: str, scenario: str = "error_spike") -> DataSource:
    if kind == "elasticsearch":
        from src.datasource.elasticsearch import ElasticsearchDataSource

        return ElasticsearchDataSource.from_config()
    return SyntheticDataSource.from_scenario(scenario)
