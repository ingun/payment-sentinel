"""
Uygulama genelindeki ayarlar. Gizli değerler .env dosyasından okunur.
Analitik eşikleri ayrıca config/thresholds.toml ile değiştirilebilir (src/analytics/thresholds.py).
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

DATA_DIR = ROOT_DIR / "data"
RUNBOOK_DIR = DATA_DIR / "runbooks"
INCIDENTS_PATH = DATA_DIR / "incidents.json"
GENERATED_DIR = DATA_DIR / "generated"  # git'e eklenmez
THRESHOLDS_PATH = Path(os.getenv("THRESHOLDS_PATH", str(ROOT_DIR / "config" / "thresholds.toml")))

# LLM ayarları: ollama | gemini | none
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "90"))

# Veri kaynağı: synthetic | elasticsearch
DATA_SOURCE = os.getenv("DATA_SOURCE", "synthetic").strip().lower()
ES_URL = os.getenv("ES_URL", "http://localhost:9200")
ES_INDEX = os.getenv("ES_INDEX", "payment-events")
ES_API_KEY = os.getenv("ES_API_KEY", "")
ES_TIMEOUT_S = float(os.getenv("ES_TIMEOUT_S", "10"))

# Sorgu maliyeti için güvenli sınırlar
MAX_BUCKETS = 500
MAX_SAMPLE_LIMIT = 50
MAX_TERMS = 20
BUCKET_MINUTES = 5
REVERSAL_LOOKBACK_MIN = 30
MAX_REVERSAL_LINKS = 5000  # Elasticsearch'te correlation bağlantısı için üst sınır

# Agent sınırları: sonsuz döngüyü engeller
MAX_TOOL_CALLS = 16
MAX_RETRIES = 2
TOOL_TIMEOUT_S = 10.0
RECURSION_LIMIT = 60

# İnceleme varsayılanları (belirsiz taleplerde kullanılır)
DEFAULT_WINDOW_MIN = 60
DEFAULT_BASELINE_MIN = 120
MAX_WINDOW_HOURS = 24


def setup_logging() -> None:
    """key=value biçiminde yapılandırılmış log çıktısı; hassas alanlar maskelenir."""
    from src.redaction import RedactingFilter

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="ts=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
    )
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
