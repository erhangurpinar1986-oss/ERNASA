"""İK Karar Merkezi: dört değerlendirme kartı + özet, masaüstü / tablet / mobil (Chromium, sentetik veri)."""
import pytest

from tests.test_ats_public_cv import intake  # noqa: F401
from tests.test_hr_decision_center_browser import (SHOTS, VIEWPORTS, _boxes_disjoint, _no_horizontal_overflow,
                                                   _open, _select, browser_app, playwright)  # noqa: F401
from tests.test_hr_flow_decision_center import flow  # noqa: F401
from tests.test_hr_flow_report import FOUR_SECTION_REPORT, NO_WEAKNESS_REPORT, _set_report

TITLES = ["Güçlü Yönler", "Zayıf Yönler", "Geliştirilmesi Gereken Yönler", "Dikkat Edilmesi Gerekenler"]
NONE = "Bu alanda doğrulanmış bulgu bulunmuyor."


def _cards(page, panel):
    return page.evaluate("""p => [...document.querySelectorAll('#hdc-panel-' + p + ' .hdc-rc')].map(c => {
        const r = c.getBoundingClientRect(), s = getComputedStyle(c.querySelector('li, p'));
        return {section: c.dataset.section, title: c.querySelector('h4').textContent, text: c.textContent,
                x: r.x, y: r.y, width: r.width, height: r.height, font: parseFloat(s.fontSize),
                visible: r.width > 0 && r.height > 0}; })""", panel)


@pytest.mark.parametrize("intake", ["sqlite"], indirect=True)
@pytest.mark.parametrize("device", ["desktop", "tablet", "mobile"])
def test_four_report_cards_are_separate_and_readable(browser_app, device):
    flow, names, token = browser_app
    _set_report(names["Ayşe Sentetik"], FOUR_SECTION_REPORT)
    _set_report(names["Burak Örnek"], NO_WEAKNESS_REPORT)  # Cem Deneme: eski üç başlıklı rapor
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=["--no-sandbox"])
        context, page, errors = _open(browser, flow["client"], token, VIEWPORTS[device])
        _select(page, "Ayşe Sentetik")
        for panel in ("overview", "report"):
            page.click(f"#hdc-tab-{panel}")
            cards = _cards(page, panel)
            assert [c["section"] for c in cards] == ["strengths", "weaknesses", "development", "attention", "summary"]
            assert [c["title"] for c in cards[:4]] == TITLES and cards[4]["title"] == "Genel İK Değerlendirme Özeti"
            assert all(c["visible"] and c["font"] >= 12 for c in cards)
            assert _boxes_disjoint(cards)
            assert cards[4]["y"] >= max(c["y"] + c["height"] for c in cards[:4]) - 1  # özet dört kartın altında
            assert "FIFO" in cards[1]["text"] and "FIFO" not in cards[2]["text"]
            assert "Kanıt: Soru 6" in cards[1]["text"]
            assert _no_horizontal_overflow(page)
        page.locator("#hdc-panel-report .hdc-report-block").scroll_into_view_if_needed()
        page.screenshot(path=str(SHOTS / f"rapor_01_dort_kart_{device}.png"))
        # 4 · Zayıf yön yoksa uydurma içerik yerine açık mesaj
        _select(page, "Burak Örnek")
        page.click("#hdc-tab-report")
        weak = [c for c in _cards(page, "report") if c["section"] == "weaknesses"][0]
        assert weak["text"] == "Zayıf Yönler" + NONE
        page.screenshot(path=str(SHOTS / f"rapor_02_zayif_yon_yok_{device}.png"))
        # 5 · Eski üç başlıklı rapor: açılır, bilgilendirme notu, zayıf yön üretilmez
        _select(page, "Cem Deneme")
        page.click("#hdc-tab-report")
        assert "dört başlıklı biçimden önce" in page.inner_text("#hdc-panel-report .hdc-legacy")
        cards = {c["section"]: c for c in _cards(page, "report")}
        assert cards["weaknesses"]["text"] == "Zayıf Yönler" + NONE
        assert "Vardiya planlama" in cards["development"]["text"]
        page.screenshot(path=str(SHOTS / f"rapor_03_eski_rapor_{device}.png"))
        # 6 · Mülakatı tamamlanmayan aday: rapor varmış gibi gösterilmez
        page.locator(".hdc-chip", has_text="Tümü").first.click()
        _select(page, "Ece Bekleyen")
        page.click("#hdc-tab-report")
        assert page.locator("#hdc-panel-report .hdc-rc").count() == 0
        assert "henüz hazır değil" in page.inner_text("#hdc-panel-report")
        page.click("#hdc-tab-overview")
        assert page.locator("#hdc-panel-overview .hdc-rc").count() == 0
        assert errors == []
        context.close()
        browser.close()
