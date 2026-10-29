"""
Çalışan uygulamadan ekran görüntüsü alır (Playwright + sistemde kurulu Microsoft Edge).

Önce uygulamayı başlatın:  streamlit run app.py --server.headless true
Sonra:                     python -m scripts.screenshots [--llm]
Geliştirme bağımlılığı:    pip install -r requirements-dev.txt
"""

import os
import sys

from playwright.sync_api import Page, sync_playwright

from src.config import ROOT_DIR

BASE = os.getenv("SCREENSHOT_BASE_URL", "http://localhost:8501")
OUT = ROOT_DIR / "screenshots"
RUN_TIMEOUT_MS = 300_000
START_BUTTON = "İncelemeyi başlat"


def investigate(page: Page, query: str, request: str | None = None) -> None:
    page.goto(f"{BASE}/?{query}")
    page.get_by_role("button", name=START_BUTTON).wait_for(timeout=60_000)
    if request:
        box = page.get_by_label("İnceleme talebi")
        box.fill(request)
        box.press("Control+Enter")
        page.wait_for_timeout(800)
    page.get_by_role("button", name=START_BUTTON).click()
    page.get_by_text("İnceleme tamamlandı").wait_for(timeout=RUN_TIMEOUT_MS)
    page.wait_for_timeout(1500)


def tab(page: Page, name: str) -> None:
    page.get_by_role("tab", name=name).click()
    page.wait_for_timeout(1200)


def shot(page: Page, name: str) -> None:
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"kaydedildi: {path.name}", flush=True)


def main(include_llm: bool) -> None:
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 2200})

        if include_llm:
            investigate(page, "scenario=bank_timeout&provider=ollama")
            tab(page, "Agent'lar")
            shot(page, "00_llm_multi_agent")
            investigate(page, "scenario=bank_timeout&provider=ollama&arch=single")
            tab(page, "İşlem izleri")
            shot(page, "00b_llm_single_agent_trace")
            tab(page, "Gerçekler ve hipotezler")
            shot(page, "00c_llm_findings")

        # Çoklu agent (varsayılan mimari)
        investigate(page, "scenario=bank_timeout&provider=none")
        shot(page, "01_bank_timeout_overview")
        tab(page, "Agent'lar")
        shot(page, "14_multi_agent_parallel")
        tab(page, "İşlem izleri")
        shot(page, "02_bank_timeout_trace")
        tab(page, "Segmentler")
        shot(page, "03_bank_timeout_segments")
        tab(page, "Gerçekler ve hipotezler")
        shot(page, "04_facts_vs_hypotheses")
        tab(page, "Benzer olaylar")
        shot(page, "05_similar_incidents")
        tab(page, "Runbook'lar")
        shot(page, "06_runbooks")

        investigate(page, "scenario=latency_then_failures&provider=none")
        tab(page, "Gecikme ve timeout")
        shot(page, "07_latency_then_failures")

        investigate(page, "scenario=version_regression&provider=none")
        tab(page, "Sürümler")
        shot(page, "08_version_regression")

        investigate(page, "scenario=reversal_anomaly&provider=none")
        tab(page, "Reversal")
        shot(page, "09_reversal_anomaly")
        tab(page, "Agent'lar")
        shot(page, "15_multi_agent_follow_up")

        # Tek agent (LangGraph) mimarisi
        investigate(page, "scenario=bank_timeout&provider=none&arch=single")
        tab(page, "İşlem izleri")
        shot(page, "16_single_agent_trace")

        investigate(page, "scenario=low_volume&provider=none")
        shot(page, "10_low_volume_insufficient")

        investigate(page, "scenario=normal_fluctuation&provider=none")
        shot(page, "11_normal_fluctuation_no_alarm")

        investigate(page, "scenario=bank_timeout&provider=none&fault=runbook_error")
        tab(page, "İşlem izleri")
        shot(page, "12_tool_failure_incomplete")

        tab(page, "Risk")
        shot(page, "13_risk_explanation")
        browser.close()


if __name__ == "__main__":
    main(include_llm="--llm" in sys.argv)
