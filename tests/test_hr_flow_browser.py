"""Actual Chromium UI smoke test for ERNASA HR FLOW.

This renders the production frontend/ats_suite.js (not a mock HTML imitation).
API responses are synthetic and intercepted; no production HR data, email or
Render service is touched. This verifies the visible user journey, not a live E2E.
"""
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


FIXTURE = """
<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ERNASA HR FLOW · Browser smoke test</title>
</head><body>
<nav><a id="poolMenu" href="#">Aday Havuzu</a></nav>
<div id="dashboardHome"><div id="atsDashboardMetrics"></div></div>
<button id="dashboardRefresh">Dashboard</button>
<section id="atsSuiteScreen" class="results-card saas-module" hidden></section>
</body></html>
"""


def test_position_screening_and_approval_real_chromium():
    with sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1366, "height": 950}, device_scale_factor=1)
        page.set_content(FIXTURE)
        page.evaluate(r"""() => {
            window.companyPortalPage = true;
            window.portalMetadataPromise = Promise.resolve({
                products: ["ats", "ai"], role: "hr_manager",
                user_id: 7, user_name: "Sentetik Yönetici"
            });
            window.__flowCalls = [];
            let permission = null;
            const candidate = {
                application_id: "application-test-1", candidate: "Sentetik Aday",
                position: "Depo Personeli", source: "synthetic_test",
                document_id: "document-test-1", has_cv: true,
                analysis_state: "analyzed", match_score: 85.0, coverage: 100,
                evidence: [{requirement:"Stok yönetimi",status:"Eşleşti"}],
                note: "Sentetik veri: test eşleşmesi.", permission:null
            };
            window.portalJson = async (url, options = {}) => {
                window.__flowCalls.push({url,method:options.method||"GET"});
                if (url === "/api/ik/ats/metrics") return {
                    candidates:1, applications:1, open_positions:1, completed_schedules:0,
                    hiring_days:null, stages:{"new":1}, months:[],
                    positions:[{name:"Depo Personeli",applications:1}]
                };
                if (url === "/api/ik/positions") return {positions:[
                    {id:"position-test-1",name:"Depo Personeli",open:1}
                ]};
                if (url === "/api/ik/assignees") return {users:[
                    {id:7,name:"Sentetik Yönetici"}
                ]};
                if (url.startsWith("/api/ik/ats/applications?")) return {total:1,applications:[
                    {id:"application-test-1",name:"Sentetik Aday",
                     position:"Depo Personeli", source:"synthetic_test",
                     stage:"new",closure:null}
                ]};
                if (url.includes("/flow/positions/position-test-1/screen?offset=0")) {
                    if(options.method!=="POST") throw Error("ATS tarama POST kullanmalıdır");
                    return {position:"Depo Personeli",total:1,limit:20,offset:0,
                        candidates:[{...candidate,permission}],
                        note:"Bu kayıtlar sentetiktir; nihai karar insana aittir."};
                }
                if (url.includes("/flow/applications/application-test-1/permission")) {
                    if(options.method!=="POST") throw Error("İzin talebi POST kullanmalıdır");
                    permission={id:"permission-test-1",status:"pending"};
                    return permission;
                }
                if (url.includes("/flow/permissions/permission-test-1/decision")) {
                    const body=JSON.parse(options.body);
                    if(body.decision!=="approved") throw Error("Geçersiz izin kararı");
                    permission={id:"permission-test-1",status:"approved"};
                    return permission;
                }
                throw Error("Beklenmeyen API çağrısı: " + url);
            };
        }""")
        page.add_style_tag(path="frontend/ernasa_v5_ik.css")
        page.add_script_tag(path="frontend/ats_suite.js")
        page.evaluate("""() => document.dispatchEvent(
            new CustomEvent("ik-portal-screen",{detail:"suite_pool"}))""")
        expect(page.get_by_role("heading",name="Aday Havuzu")).to_be_visible()
        page.locator('select[name="position_id"]').first.select_option("position-test-1")
        page.get_by_role("button",name="CV'leri Getir ve ATS Analizi Yap").click()
        expect(page.get_by_text("85 / 100")).to_be_visible()
        expect(page.get_by_text("Kanıt kapsamı: %100")).to_be_visible()
        expect(page.get_by_text("Stok yönetimi — Eşleşti")).to_be_visible()
        page.get_by_role("button",name="Mülakat İçin İzin İste").click()
        expect(page.get_by_text("Mülakat izni bekliyor")).to_be_visible()
        page.get_by_role("button",name="İzni Onayla").click()
        expect(page.get_by_text("Mülakat izni onaylandı")).to_be_visible()
        calls=page.evaluate("window.__flowCalls")
        assert any(v["method"]=="POST" and "/flow/positions/" in v["url"] for v in calls)
        assert sum("/permission" in v["url"] and v["method"]=="POST" for v in calls)==2
        assert not any("/interview-links/" in v["url"] or "/email" in v["url"] for v in calls)
        out=Path("artifacts")
        out.mkdir(exist_ok=True)
        page.screenshot(path=str(out/"ernasa_hr_flow_chromium_ui.png"),full_page=True)
        browser.close()


def test_cv_selection_saves_and_displays_ats_without_another_click():
    with sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True,args=["--no-sandbox"])
        page = browser.new_page()
        page.set_content(FIXTURE)
        page.evaluate("""() => {
            window.companyPortalPage = true;
            window.portalMetadataPromise = Promise.resolve({products:["ats"],role:"hr_manager",user_id:7});
            window.calls = [];
            window.portalJson = async (url,options) => {
                const method = options.method || "GET";
                window.calls.push({url,method});
                if (url.endsWith("/positions")) return {positions:[{id:"p1",name:"Lojistik",open:1}]};
                if (url.endsWith("/assignees")) return {users:[]};
                if (url.includes("/ats/applications?")) return {total:1,applications:[
                    {id:"a1",name:"Deniz Eren Karaca",email:"shared@example.test",
                     position:"Lojistik",source:"CV Yükleme",stage:"new"}
                ]};
                if (url.endsWith("/cv-import/preview")) return {import_id:"preview",name:"Deniz Eren Karaca",
                    email:"shared@example.test",phone:"05310000000",suggested_position_id:"p1",
                    education:["Lojistik ön lisans"],experience:["Stok takibi"]};
                if (url.endsWith("/cv-import/save")) {
                    const selection = JSON.parse(options.body.get("selection"));
                    if (selection.position_id !== "p1" || selection.name !== "Deniz Eren Karaca")
                        throw Error("Otomatik doldurma başarısız");
                    return {status:"saved",application_id:"a1",document_id:"d1",identity_conflict:true};
                }
                if (url.endsWith("/documents/d1/screening")) return {state:"analyzed",score:100,coverage:100,
                    strengths:[{requirement:"Stok takibi",status:"Eşleşti"}],development:[]};
                return {};
            };
        }""")
        page.add_script_tag(path="frontend/ats_suite.js")
        page.evaluate('''() => document.dispatchEvent(
            new CustomEvent("ik-portal-screen",{detail:"suite_pool"}))''')
        page.get_by_role("button",name="CV Yükle",exact=True).click()
        page.locator('input[type="file"]').set_input_files({
            "name":"deniz.pdf","mimeType":"application/pdf","buffer":b"%PDF-1.7 synthetic"})
        expect(page.get_by_text("ATS puanı · 100")).to_be_visible()
        expect(page.get_by_text("Lojistik ön lisans",exact=True)).to_be_visible()
        expect(page.get_by_text("Ayrı kaydedildi",exact=False)).to_be_visible()
        calls = page.evaluate("window.calls")
        assert sum(call["url"].endswith("/cv-import/save") for call in calls) == 1
        assert not any("/flow/positions/" in call["url"] for call in calls)
        page.get_by_role("button",name="Ad Soyad",exact=True).click()
        expect(page.locator('th[aria-sort="ascending"]')).to_be_visible()
        page.get_by_role("button",name="Ad Soyad",exact=True).click()
        expect(page.locator('th[aria-sort="descending"]')).to_be_visible()
        browser.close()
