"""
LLM sağlayıcı seçimi ve erişilebilirlik kontrolü.

LLM yalnızca talep yorumlama, plan, araç seçimi ve bulgu özeti için kullanılır.
Sayısal hesaplar her zaman Python araçlarında yapılır.
"""

import json
import logging
import urllib.request
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel

from src import config

logger = logging.getLogger(__name__)


@dataclass
class LLMStatus:
    provider: str
    model: str | None
    available: bool
    message: str


def check_ollama(base_url: str, model: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=timeout) as resp:
            models = [m["name"] for m in json.load(resp).get("models", [])]
    except OSError as exc:
        return False, f"Ollama'ya ulaşılamadı ({base_url}): {exc}"
    if model not in models:
        return False, f"Ollama modeli bulunamadı: {model}. Çalıştırın: ollama pull {model}"
    return True, f"Ollama hazır ({model})"


def get_llm(provider: str | None = None) -> tuple[BaseChatModel | None, LLMStatus]:
    """Yapılandırılmış LLM'i döner; kullanılamıyorsa (None, durum) döner ve agent deterministik moda geçer."""
    provider = (provider or config.LLM_PROVIDER).lower()
    if provider == "none":
        return None, LLMStatus("none", None, False, "LLM devre dışı (LLM_PROVIDER=none). Deterministik demo modu.")

    if provider == "gemini":
        if not config.GEMINI_API_KEY:
            return None, LLMStatus("gemini", config.GEMINI_MODEL, False, "GEMINI_API_KEY tanımlı değil. Deterministik demo modu.")
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(
            model=config.GEMINI_MODEL, api_key=config.GEMINI_API_KEY, temperature=0, timeout=config.LLM_TIMEOUT_S, max_retries=1
        )
        return llm, LLMStatus("gemini", config.GEMINI_MODEL, True, f"Gemini hazır ({config.GEMINI_MODEL})")

    if provider == "ollama":
        ok, message = check_ollama(config.OLLAMA_BASE_URL, config.OLLAMA_MODEL)
        if not ok:
            return None, LLMStatus("ollama", config.OLLAMA_MODEL, False, message + " Deterministik demo modu.")
        from langchain_ollama import ChatOllama

        llm = ChatOllama(
            model=config.OLLAMA_MODEL,
            base_url=config.OLLAMA_BASE_URL,
            temperature=0,
            reasoning=False,
            client_kwargs={"timeout": config.LLM_TIMEOUT_S},
        )
        return llm, LLMStatus("ollama", config.OLLAMA_MODEL, True, message)

    return None, LLMStatus(provider, None, False, f"Bilinmeyen LLM_PROVIDER: {provider}. Deterministik demo modu.")
