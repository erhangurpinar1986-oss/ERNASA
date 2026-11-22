"""Tam otomatik HR FLOW: aday ve İK ekranlarının gerçek Chromium kabul testi (sentetik veri)."""
import re

import pytest

import hr_flow_decisions
from tests.test_ats_public_cv import intake  # noqa: F401
from tests.test_hr_decision_center_browser import SHOTS, VIEWPORTS, _forwarder, _no_horizontal_overflow, playwright
from tests.test_hr_flow_automation import _complete_interview, _invitation_token, automated  # noqa: F401
from tests.test_hr_flow_decision_center import flow  # noqa: F401


@pytest.fixture
def scene(automated, monkeypatch):  # noqa: F811
    a = automated
    main, actor = a["main"], a["actor"]
    identity = {"company_id": actor.company_id, "role": "hr_manager", "version": actor.access_version,
                "company_name": "Sentetik Firma", "user_name": "Sentetik Yönetici", "products": ["ats", "ai"],
                "month_bounds": ["2026-11-01T00:00:00+00:00", "2026-12-01T00:00:00+00:00"],
                "interview_quota": None, "permissions": []}
    previous = main.portal_access.control_call

    def control_call(portal, operation, **kwargs):
        if operation == "session":
            return dict(identity)
        if operation == "assignees":
            return {"users": [{"id": actor.user_id, "name": "Sentetik Yönetici"}]}
        return previous(portal, operation, **kwargs)

    monkeypatch.setattr(main.portal_access, "control_call", control_call)
    done = a["apply"]("Melis Tamam", "melis.tamam@example.test")
    booked = a["apply"]("Nazlı Randevu", "nazli.randevu@example.test")
    waiting = a["apply"]("Okan Seçecek", "okan.secer@example.test")
    a["screen"]()
    a["book"]()
    for email in ("melis.tamam@example.test", "nazli.randevu@example.test"):
        url = a["booking_url"](email)
        _, options = a["slots"](url)
        a["client"].post(url, data={"starts_at": options[2 if "melis" in email else 6]})
    a["book"]()
    token = _invitation_token(a, "melis.tamam@example.test")
    _complete_interview(a, token, done)
    a["notices"]()
    session = main.portal_access.new_session(a["portal"], {"company_id": actor.company_id, "user_id": actor.user_id,
                                                           "version": actor.access_version})
    tracking = re.search(r"/basvuru-takip/([A-Za-z0-9_-]{43})",
                         next(n for n in a["sent"] if "melis" in n["to"])["body"]).group(1)
    return {"a": a, "session": session, "done": done, "booked": booked, "waiting": waiting,
            "urls": {k: a["booking_url"](e) for k, e in (("booked", "nazli.randevu@example.test"),
                                                        ("waiting", "okan.secer@example.test"))},
            "tracking": tracking}


def _page(browser, scene, size):
    context = browser.new_context(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/*", _forwarder(scene["a"]["client"], scene["session"]))
    return context, page, errors


@pytest.mark.parametrize("intake", ["sqlite"], indirect=True)
def test_candidate_pages_show_8_hour_window_on_all_devices(scene):
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=["--no-sandbox"])
        for device in ("desktop", "mobile"):
            context, page, errors = _page(browser, scene, VIEWPORTS[device])
            page.goto("https://synthetic.ernasa.com" + scene["urls"]["waiting"])
            notice = page.locator("section.info")
            assert notice.is_visible()
            assert "Mülakatınız Hakkında Önemli Bilgi" in notice.inner_text()
            assert "8 saat" in notice.inner_text()
            assert page.locator("select#starts_at option").count() > 5
            assert page.locator("label[for=starts_at]").count() == 1  # ekran okuyucu etiketi
            assert _no_horizontal_overflow(page)
            page.screenshot(path=str(SHOTS / f"auto_01_saat_secimi_{device}.png"), full_page=True)
            page.goto("https://synthetic.ernasa.com" + scene["urls"]["booked"])
            text = page.inner_text("main")
            assert "Mevcut randevunuz" in text and "Erişim bitişi" in text
            assert page.get_by_role("button", name="Randevuyu değiştir").is_visible()
            assert page.get_by_role("button", name="Randevuyu iptal et").is_visible()
            assert _no_horizontal_overflow(page)
            page.screenshot(path=str(SHOTS / f"auto_02_randevu_degistir_{device}.png"), full_page=True)
            page.goto("https://synthetic.ernasa.com/basvuru-takip/" + scene["tracking"])
            assert "Başvuru takibi" in page.inner_text("h1")
            assert page.locator("[role=status]").first.is_visible()
            page.keyboard.press("Tab")
            assert _no_horizontal_overflow(page)
            page.screenshot(path=str(SHOTS / f"auto_03_basvuru_takip_{device}.png"), full_page=True)
            assert errors == []
            context.close()
        browser.close()


@pytest.mark.parametrize("intake", ["sqlite"], indirect=True)
def test_hr_screens_automation_settings_reports_and_notifications(scene):
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=["--no-sandbox"])
        for device in ("desktop", "tablet"):
            context, page, errors = _page(browser, scene, VIEWPORTS[device])
            page.goto("https://synthetic.ernasa.com/ik-v5")
            notice = page.locator("#ernasa-legal-notice button", has_text="Kapat")
            if notice.count():
                notice.first.click()
            page.click("#selinIntakeMenu")
            page.wait_for_selector(".hdc-row", timeout=15000)
            # Üç panel korunuyor
            assert page.locator(".hdc-left").is_visible() and page.locator(".hdc-center").is_visible()
            assert page.locator(".hdc-right").is_visible()
            page.locator(".hdc-row", has_text="Melis Tamam").click()
            page.wait_for_function("() => document.querySelector('.hdc-cand h3')?.textContent === 'Melis Tamam'")
            page.click("#hdc-tab-notifications")
            panel = page.locator("#hdc-panel-notifications")
            assert "Aday bilgilendirmeleri (otomatik akış)" in panel.text_content()
            assert "Sağlayıcı kabul etti" in panel.text_content()
            page.screenshot(path=str(SHOTS / f"auto_04_bildirimler_{device}.png"))
            page.locator("details.flow-automation summary").click()
            page.wait_for_selector(".hdc-autoform input[name=enabled]")
            assert page.locator(".hdc-autoform input[name=enabled]").is_checked()
            page.locator("details.flow-automation").scroll_into_view_if_needed()
            page.screenshot(path=str(SHOTS / f"auto_05_otomasyon_ayari_{device}.png"), full_page=True)
            page.locator("details.flow-reports summary").click()
            page.wait_for_selector(".hdc-kpis")
            assert "Tamamlanan mülakat" in page.text_content(".hdc-kpis")
            page.locator("details.flow-reports").scroll_into_view_if_needed()
            page.screenshot(path=str(SHOTS / f"auto_06_raporlar_{device}.png"), full_page=True)
            assert _no_horizontal_overflow(page)
            assert errors == []
            context.close()
        browser.close()
    assert hr_flow_decisions.list_center(scene["a"]["actor"])["counts"]["awaiting_hr"] == 1
