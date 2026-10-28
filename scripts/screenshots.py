"""
Çalışan uygulamadan ekran görüntüsü alır (Playwright + sistemde kurulu Microsoft Edge).

Önce uygulamayı başlatın:  streamlit run app.py --server.headless true
Sonra:                     python -m scripts.screenshots
Geliştirme bağımlılığı:    pip install -r requirements-dev.txt
"""

import sys

from playwright.sync_api import Page, sync_playwright

from src.config import ROOT_DIR

BASE = "http://localhost:8501"
OUT = ROOT_DIR / "screenshots"
RUN_TIMEOUT_MS = 300_000


def investigate(page: Page, query: str, request: str | None = None) -> None:
    page.goto(f"{BASE}/?{query}")
    page.get_by_role("button", name="Investigate Incident").wait_for(timeout=60_000)
    if request:
        box = page.get_by_label("İnceleme talebi")
        box.fill(request)
        box.press("Control+Enter")
    page.get_by_role("button", name="Investigate Incident").click()
    page.get_by_text("Risk:", exact=False).first.wait_for(timeout=RUN_TIMEOUT_MS)
    page.wait_for_timeout(1500)


def tab(page: Page, name: str) -> None:
    page.get_by_role("tab", name=name).click()
    page.wait_for_timeout(1200)


def shot(page: Page, name: str) -> None:
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"kaydedildi: {path.name}", flush=True)


def section(page: Page, text: str, name: str) -> None:
    """Streamlit içeriği kendi kapsayıcısında kaydığı için full_page alt bölümlere inmez; bölüme kaydırıp çeker."""
    page.get_by_text(text, exact=False).first.scroll_into_view_if_needed()
    page.wait_for_timeout(800)
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path))
    print(f"kaydedildi: {path.name}", flush=True)


def main(include_llm: bool) -> None:
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 2400})

        if include_llm:
            investigate(page, "scenario=error_spike&provider=ollama")
            shot(page, "01_llm_error_spike_overview")
            tab(page, "🔎 Agent izleri")
            shot(page, "02_llm_agent_trace")
            tab(page, "📄 Rapor")
            shot(page, "03_llm_incident_report")
            section(page, "Agent bulgu özeti", "03b_llm_agent_findings")

        investigate(page, "scenario=error_spike&provider=none")
        tab(page, "📈 Zaman serisi")
        shot(page, "04_error_spike_timeseries")
        tab(page, "🧩 Paternler")
        shot(page, "05_error_spike_patterns")
        tab(page, "🕘 Benzer olaylar")
        shot(page, "06_error_spike_similar_incidents")
        tab(page, "📚 Runbook kaynakları")
        shot(page, "07_error_spike_runbooks")

        investigate(page, "scenario=latency_degradation&provider=none")
        shot(page, "08_latency_degradation_overview")
        tab(page, "⚠️ Hatalar ve servisler")
        shot(page, "09_latency_services")

        investigate(page, "scenario=volume_drop&provider=none")
        tab(page, "📈 Zaman serisi")
        shot(page, "10_volume_drop_timeseries")

        investigate(page, "scenario=normal&provider=none")
        shot(page, "11_normal_no_false_alarm")

        investigate(page, "scenario=error_spike&provider=none&fault=runbook_error")
        tab(page, "🔎 Agent izleri")
        shot(page, "12_tool_failure_handling")

        investigate(page, "scenario=latency_degradation&provider=none", "Son 45 dakikadaki ödeme hatalarını araştır")
        investigate_follow_up(page)
        tab(page, "🧠 Hafıza")
        shot(page, "13_follow_up_memory")
        browser.close()


def investigate_follow_up(page: Page) -> None:
    """Aynı oturumda takip sorusu (sayfa yenilenmez; hafıza korunur)."""
    box = page.get_by_label("İnceleme talebi")
    box.fill("Şimdi sadece acquirer-gateway servisine bak")
    box.press("Control+Enter")
    page.wait_for_timeout(800)
    page.get_by_role("button", name="Investigate Incident").click()
    page.get_by_text("İnceleme tamamlandı").wait_for(timeout=RUN_TIMEOUT_MS)
    page.wait_for_timeout(1500)


if __name__ == "__main__":
    main(include_llm="--no-llm" not in sys.argv)
