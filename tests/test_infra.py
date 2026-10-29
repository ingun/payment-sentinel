"""Altyapı testleri: LLM sağlayıcı seçimi, depo fabrikası, log maskeleme ve talep ayrıştırma kenar durumları."""

import io
import json
import logging

import pytest

from src import config, llm
from src.agent import InvestigationAgent
from src.redaction import RedactingFilter
from src.repositories import SyntheticPaymentRepository, create_repository

# ---------- LLM sağlayıcı seçimi (gerçek ağ çağrısı yok) ----------


def test_llm_none_and_unknown_provider():
    model, status = llm.get_llm("none")
    assert model is None and not status.available
    model, status = llm.get_llm("openai-gpt")
    assert model is None and "Bilinmeyen" in status.message


def test_gemini_without_key_falls_back(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    model, status = llm.get_llm("gemini")
    assert model is None and "GEMINI_API_KEY" in status.message


def test_ollama_unreachable_falls_back(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(llm.urllib.request, "urlopen", refuse)
    model, status = llm.get_llm("ollama")
    assert model is None and "ulaşılamadı" in status.message and "Deterministik" in status.message


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_ollama_model_missing_and_available(monkeypatch):
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: _Resp(json.dumps({"models": [{"name": "other:1b"}]}).encode()))
    ok, message = llm.check_ollama("http://x", "qwen3.5:4b")
    assert not ok and "ollama pull" in message
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: _Resp(json.dumps({"models": [{"name": "qwen3.5:4b"}]}).encode()))
    ok, _ = llm.check_ollama("http://x", "qwen3.5:4b")
    assert ok


def test_ollama_available_returns_model(monkeypatch):
    monkeypatch.setattr(llm, "check_ollama", lambda *_: (True, "hazır"))
    model, status = llm.get_llm("ollama")
    assert model is not None and status.available and status.provider == "ollama"


# ---------- Depo fabrikası ----------


def test_create_repository_synthetic_and_missing_es_client(monkeypatch):
    repo = create_repository("synthetic", "normal_baseline")
    assert isinstance(repo, SyntheticPaymentRepository) and repo.label == "normal_baseline"
    import builtins

    real_import = builtins.__import__

    def no_es(name, *args, **kwargs):
        if name == "elasticsearch":
            raise ImportError("yok")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_es)
    with pytest.raises(RuntimeError, match="requirements-es"):
        create_repository("elasticsearch")


def test_repository_from_jsonl(tmp_path):
    from src.data_generator import generate_scenario

    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in generate_scenario("normal_baseline")[:200]), encoding="utf-8")
    repo = SyntheticPaymentRepository.from_jsonl(path)
    assert repo.label == "s" and len(repo.df) == 200


# ---------- Log maskeleme ----------


def test_redacting_filter_masks_log_messages():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "pan=%s mail=%s", ("4111 1111 1111 1111", "a@b.com"), None)
    assert RedactingFilter().filter(record)
    assert "4111 1111" not in record.getMessage() and "****1111" in record.getMessage()
    assert "a@b.com" not in record.getMessage()


def test_setup_logging_installs_filter_once():
    config.setup_logging()
    config.setup_logging()
    for handler in logging.getLogger().handlers:
        assert sum(isinstance(f, RedactingFilter) for f in handler.filters) <= 1


# ---------- Talep ayrıştırma kenar durumları ----------


def test_invalid_clock_range_falls_back_to_default(make_toolkit):
    state = InvestigationAgent(make_toolkit("normal_baseline")).run("13:30-12:00 arası ödeme hatalarını incele", thread_id="c")
    assert state["scope"]["window_minutes"] == config.DEFAULT_WINDOW_MIN
    assert any("saat aralığı geçersiz" in a for a in state["assumptions"])


def test_valid_clock_range_is_used(make_toolkit):
    state = InvestigationAgent(make_toolkit("normal_baseline")).run("10:30-11:45 arası ödeme hatalarını incele", thread_id="c")
    assert (state["scope"]["current_start"], state["scope"]["current_end"]) == ("2026-10-27T10:30:00Z", "2026-10-27T11:45:00Z")


def test_out_of_range_window_is_corrected(make_toolkit):
    state = InvestigationAgent(make_toolkit("normal_baseline")).run("Son 2 dakikadaki ödeme hatalarını incele", thread_id="c")
    assert state["scope"]["window_minutes"] == config.DEFAULT_WINDOW_MIN
    assert any("geçerli aralıkta değil" in a for a in state["assumptions"])
