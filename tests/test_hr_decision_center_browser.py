"""Gerçek Chromium ile İK Karar Merkezi kabul testi.

Gerçek IK V5 sayfası (frontend/ernasa_v5_ik.html + ats_suite.js + hr_decision_center.js)
açılır. Tarayıcının tüm istekleri gerçek FastAPI uygulamasına (TestClient) iletilir:
firma portalı oturumu, Origin kontrolü, ATS / HR FLOW / takvim / Akademi kayıtları
gerçektir. Yalnızca CONTROL kimlik servisi ve e-posta sağlayıcısı sentetiktir.
Üretim verisi, e-postası veya Render kullanılmaz.
"""
import os
from contextlib import closing
from pathlib import Path

import pytest

import database
from tests.test_ats_public_cv import intake  # noqa: F401
from tests.test_hr_flow_decision_center import flow  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")
SHOTS = Path(os.getenv("HDC_SCREENSHOT_DIR", "artifacts"))
VIEWPORTS = {"desktop": (1440, 900), "tablet": (1024, 768), "mobile": (390, 844)}


def _forwarder(client, cookie):
    def handle(route):
        request = route.request
        if not request.url.startswith("https://synthetic.ernasa.com/"):
            return route.abort()
        path = request.url.split("https://synthetic.ernasa.com", 1)[1]
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() in ("content-type", "origin", "accept")}
        headers["cookie"] = "ernasa_company_session=" + cookie
        response = client.request(request.method, path, headers=headers, content=request.post_data_buffer)
        out = {k: v for k, v in response.headers.items()
               if k.lower() not in ("content-length", "content-encoding", "transfer-encoding", "set-cookie")}
        route.fulfill(status=response.status_code, headers=out, body=response.content)
    return handle


@pytest.fixture
def browser_app(flow, monkeypatch):  # noqa: F811
    main, actor = flow["main"], flow["actor"]
    names = {}
    for name, email in (("Ayşe Sentetik", "ayse@example.test"), ("Burak Örnek", "burak@example.test"),
                        ("Cem Deneme", "cem@example.test"), ("Deniz Kurgusal", "deniz@example.test")):
        names[name] = flow["new"](name, email)[0]
    names["Ece Bekleyen"] = flow["new"]("Ece Bekleyen", "ece@example.test", complete=False)[0]
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
    token = main.portal_access.new_session(flow["portal"], {"company_id": actor.company_id,
                                                            "user_id": actor.user_id, "version": actor.access_version})
    return flow, names, token


def _open(browser, client, token, size):
    width, height = size
    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/*", _forwarder(client, token))
    page.goto("https://synthetic.ernasa.com/ik-v5")
    # Sitenin KVKK bilgilendirmesi: gerçek kullanıcı gibi "Kapat" ile kapatılır
    notice = page.locator("#ernasa-legal-notice button", has_text="Kapat")
    if notice.count():
        notice.first.click()
    if size[0] <= 820:
        page.locator("#mobileMenuButton, .mobile-menu-toggle").first.click() if page.locator("#mobileMenuButton, .mobile-menu-toggle").count() else None
    page.wait_for_selector("#selinIntakeMenu", state="visible", timeout=15000)
    page.click("#selinIntakeMenu")
    page.wait_for_selector(".hdc-row", timeout=15000)
    return context, page, errors


def _no_horizontal_overflow(page):
    return page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")


def _boxes_disjoint(boxes):
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if not (a["x"] + a["width"] <= b["x"] + 0.5 or b["x"] + b["width"] <= a["x"] + 0.5 or
                    a["y"] + a["height"] <= b["y"] + 0.5 or b["y"] + b["height"] <= a["y"] + 0.5):
                return False
    return True


def _button_boxes(page):
    """Dört karar butonunun konumunu aynı anda ölçer (kaydırma sırasında tutarlı olsun)."""
    page.wait_for_timeout(600)  # mobilde karar paneline yumuşak kaydırma tamamlansın
    return page.evaluate("""[...document.querySelectorAll('.hdc-actions .hdc-btn')].slice(0,4).map(b => {
        const r = b.getBoundingClientRect(); return {x:r.x, y:r.y, width:r.width, height:r.height}; })""")


def _select(page, name):
    page.locator(".hdc-row", has_text=name).click()
    page.wait_for_function("n => document.querySelector('.hdc-cand h3')?.textContent === n", arg=name)


@pytest.mark.parametrize("intake", ["sqlite"], indirect=True)
def test_three_panel_decision_center_desktop(browser_app):
    flow, names, token = browser_app
    SHOTS.mkdir(parents=True, exist_ok=True)
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=["--no-sandbox"])
        context, page, errors = _open(browser, flow["client"], token, VIEWPORTS["desktop"])
        # Varsayılan filtre: İK kararı bekleyenler (4 tamamlanmış aday), bekleyen aday listede yok
        assert page.locator(".hdc-row").count() == 4
        assert page.locator(".hdc-chip[aria-pressed=true]").inner_text().endswith("Karar bekliyor")
        left, center, right = (page.locator(sel).bounding_box() for sel in (".hdc-left", ".hdc-center", ".hdc-right"))
        assert left["x"] < center["x"] < right["x"] and abs(left["y"] - right["y"]) < 2
        _select(page, "Ayşe Sentetik")
        assert page.locator(".hdc-row[aria-current=true]").inner_text().startswith("Ayşe Sentetik")
        assert page.locator(".hdc-cand .hdc-pill").inner_text() == "İK kararı bekliyor"
        assert page.locator(".hdc-score b").nth(1).inner_text() == "72"
        page.screenshot(path=str(SHOTS / "hdc_01_masaustu_genel.png"))
        # Karar butonları üst üste binmez ve sağ panelde kalır
        buttons = _button_boxes(page)
        right = page.locator(".hdc-right").bounding_box()
        assert _boxes_disjoint(buttons)
        assert all(b["x"] >= right["x"] and b["x"] + b["width"] <= right["x"] + right["width"] + 1 for b in buttons)
        assert _no_horizontal_overflow(page)

        # Sekmeler: yalnızca ilgili içerik görünür; uzun içerik kendi alanında kayar, sayfa uzamaz
        height_before = page.evaluate("document.documentElement.scrollHeight")
        expectations = {"cv": ".hdc-cv-tools", "ats": "p", "report": ".hdc-rc-weak", "qa": ".hdc-qa",
                        "history": ".hdc-timeline", "training": "p", "notifications": ".hdc-table"}
        for tab, selector in expectations.items():
            page.click(f"#hdc-tab-{tab}")
            assert page.locator("[role=tabpanel]:not([hidden])").count() == 1
            assert page.locator(f"#hdc-panel-{tab}").is_visible()
            assert page.locator(f"#hdc-panel-{tab} {selector}").first.is_visible()
        page.click("#hdc-tab-qa")
        assert page.locator("#hdc-panel-qa .hdc-qa").count() == 10
        assert page.evaluate("(()=>{const p=document.querySelector('.hdc-tabpanels');return p.scrollHeight>p.clientHeight})()")
        assert page.evaluate("document.documentElement.scrollHeight") == height_before
        page.screenshot(path=str(SHOTS / "hdc_02_masaustu_sorular.png"))
        page.click("#hdc-tab-history")
        assert page.locator("#hdc-panel-history .hdc-step-ok", has_text="Aday uygunluk bağlantısını açtı").count() == 1
        assert page.locator("#hdc-panel-history .hdc-step-ok", has_text="Aday mülakatı başlattı").count() == 1
        page.screenshot(path=str(SHOTS / "hdc_03_masaustu_surec.png"))

        # CV önizlemesi: sınırlı yükseklik, tam ekran seçeneği, sekme değişiminde yeniden oluşturulmaz
        page.click("#hdc-tab-cv")
        page.wait_for_selector("iframe.hdc-cv-frame")
        assert page.locator("iframe.hdc-cv-frame").get_attribute("src").startswith("blob:")
        assert page.locator("button", has_text="Tam ekran").is_visible()
        assert page.locator("iframe.hdc-cv-frame").bounding_box()["height"] <= 0.75 * 900
        page.evaluate("document.querySelector('iframe.hdc-cv-frame').dataset.marker='same-element'")
        page.click("#hdc-tab-report")
        page.click("#hdc-tab-cv")
        assert page.evaluate("document.querySelector('iframe.hdc-cv-frame').dataset.marker") == "same-element"
        page.screenshot(path=str(SHOTS / "hdc_04_masaustu_cv.png"))

        # Klavye: WAI-ARIA sekme kalıbı
        page.focus("#hdc-tab-cv")
        page.keyboard.press("ArrowRight")
        assert page.locator("#hdc-tab-ats").get_attribute("aria-selected") == "true"
        page.keyboard.press("End")
        assert page.evaluate("document.activeElement.id") == "hdc-tab-notifications"
        page.keyboard.press("Home")
        assert page.locator("#hdc-tab-overview").get_attribute("aria-selected") == "true"

        # REDDET: gerekçesiz gönderilemez, Esc kapatır ve odak geri döner
        page.click(".hdc-reject")
        page.wait_for_selector(".hdc-dialog")
        page.screenshot(path=str(SHOTS / "hdc_05_ret_penceresi.png"))
        page.click(".hdc-dialog footer .hdc-reject")
        assert page.locator(".hdc-dialog").count() == 1
        page.keyboard.press("Escape")
        assert page.locator(".hdc-dialog").count() == 0
        assert page.evaluate("document.activeElement.textContent") == "REDDET"
        page.click(".hdc-reject")
        page.fill(".hdc-dialog textarea[name=reason]", "Vardiya düzenine uygun değil.")
        page.click(".hdc-dialog footer .hdc-reject")
        page.wait_for_selector(".hdc-dialog", state="detached")
        page.wait_for_function("document.querySelector('.hdc-cand .hdc-pill')?.textContent === 'Reddedildi'")
        assert page.locator(".hdc-actions .hdc-btn:disabled").count() == 4

        # ONAYLA
        page.click(".hdc-chip:has-text('Karar bekliyor')")
        _select(page, "Burak Örnek")
        page.click(".hdc-approve")
        page.click(".hdc-dialog footer .hdc-approve")
        page.wait_for_function("document.querySelector('.hdc-cand .hdc-pill')?.textContent === 'Onaylandı · teklif aşaması'")

        # İKİNCİ GÖRÜŞME (takvimle) → ardından EĞİTİM ATA
        page.click(".hdc-chip:has-text('Karar bekliyor')")
        _select(page, "Cem Deneme")
        page.click(".hdc-second")
        page.select_option(".hdc-dialog select[name=interviewer_id]", index=1)
        page.fill(".hdc-dialog input[name=starts_at]", "2026-12-03T10:00")
        page.select_option(".hdc-dialog select[name=kind]", "onsite")
        page.click(".hdc-dialog footer .hdc-second")
        page.wait_for_function("document.querySelector('.hdc-cand .hdc-pill')?.textContent === 'İkinci görüşme'")
        page.click(".hdc-training")
        page.wait_for_selector(".hdc-dialog input[name=module_ids]")
        page.screenshot(path=str(SHOTS / "hdc_06_egitim_penceresi.png"))
        page.click(".hdc-dialog footer .hdc-training")
        page.wait_for_function("document.querySelector('.hdc-cand .hdc-pill')?.textContent === 'Eğitim atandı'")
        page.click("#hdc-tab-training")
        assert page.locator("#hdc-panel-training .hdc-progress").is_visible()
        assert page.locator("button", has_text="Eğitim davetini gönder").is_visible()
        page.screenshot(path=str(SHOTS / "hdc_07_egitim_ilerleme.png"))

        # Sayaçlar güncellendi; bekleyen adayın adımları tamamlanmış gösterilmiyor
        page.click(".hdc-chip:has-text('Süreçte')")
        _select(page, "Ece Bekleyen")
        assert page.locator(".hdc-actions .hdc-btn:disabled").count() == 4
        page.click("#hdc-tab-history")
        assert page.locator("#hdc-panel-history .hdc-step-ok", has_text="Aday mülakatı başlattı").count() == 0
        page.screenshot(path=str(SHOTS / "hdc_08_bekleyen_aday.png"))
        assert not errors, errors
        context.close()
        browser.close()
    with closing(database.get_connection()) as db:
        rows = db.cursor().execute("SELECT decision FROM flow_hr_decisions ORDER BY decided_at").fetchall()
    assert [row["decision"] for row in rows] == ["reject", "approve", "second_interview", "assign_training"]
    assert not [o for o in flow["outgoing"] if o[1] in ("ayse@example.test", "burak@example.test", "cem@example.test")
                and o[0] not in ("availability", "invitation")]


@pytest.mark.parametrize("intake", ["sqlite"], indirect=True)
@pytest.mark.parametrize("size", ["tablet", "mobile"])
def test_decision_center_responsive(browser_app, size):
    flow, names, token = browser_app
    SHOTS.mkdir(parents=True, exist_ok=True)
    width, height = VIEWPORTS[size]
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=["--no-sandbox"])
        context, page, errors = _open(browser, flow["client"], token, VIEWPORTS[size])
        _select(page, "Deniz Kurgusal")
        assert _no_horizontal_overflow(page)
        buttons = _button_boxes(page)
        assert _boxes_disjoint(buttons)
        assert all(b["x"] >= 0 and b["x"] + b["width"] <= width + 1 for b in buttons)
        center, right = page.locator(".hdc-center").bounding_box(), page.locator(".hdc-right").bounding_box()
        assert right["y"] < center["y"]  # karar alanı aday dosyasının üstünde, sürekli erişilebilir
        page.click("#hdc-tab-qa")
        assert page.locator("#hdc-panel-qa .hdc-qa").count() == 10
        assert _no_horizontal_overflow(page)
        page.screenshot(path=str(SHOTS / f"hdc_09_{size}.png"), full_page=(size == "mobile"))
        assert not errors, errors
        context.close()
        browser.close()
