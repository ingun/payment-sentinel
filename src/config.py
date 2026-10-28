"""
Uygulama genelindeki ayarlar. Gizli değerler .env dosyasından okunur.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

DATA_DIR = ROOT_DIR / "data"
SCENARIO_DIR = DATA_DIR / "scenarios"
RUNBOOK_DIR = DATA_DIR / "runbooks"
SAMPLE_LOGS_PATH = DATA_DIR / "sample_logs.jsonl"

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
ES_INDEX = os.getenv("ES_INDEX", "payment-logs")
ES_API_KEY = os.getenv("ES_API_KEY", "")

# Sorgu maliyeti için güvenli sınırlar
MAX_BUCKETS = 500
MAX_SAMPLE_LIMIT = 50
MAX_TERMS = 20
BUCKET_MINUTES = 5

# Agent sınırları: sonsuz döngüyü engeller
MAX_TOOL_CALLS = 12
MAX_RETRIES = 2
TOOL_TIMEOUT_S = 10.0
RECURSION_LIMIT = 40

# İnceleme varsayılanları (belirsiz taleplerde kullanılır)
DEFAULT_WINDOW_MIN = 30
DEFAULT_BASELINE_MIN = 120
MAX_WINDOW_HOURS = 24

# Bu değerin altındaki örneklemde risk skoru üretilmez
MIN_SAMPLES = 30

SERVICES = [
    "api-gateway",
    "payment-orchestrator",
    "acquirer-gateway",
    "provider-connector",
    "fraud-check",
]

KNOWN_ERROR_CODES = [
    "CARD_DECLINED",
    "INSUFFICIENT_FUNDS",
    "HTTP_500",
    "HTTP_502",
    "GATEWAY_504",
    "PROVIDER_TIMEOUT",
    "CONNECTION_RESET",
]


def setup_logging() -> None:
    """key=value biçiminde yapılandırılmış log çıktısı."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="ts=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
    )
