"""AI mülakat raporu: dört ayrı değerlendirme alanı (Güçlü / Zayıf / Geliştirilmesi Gereken /
Dikkat Edilmesi Gerekenler) + Genel İK Değerlendirme Özeti. Sentetik veriyle doğrulama."""
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import database
import hr_flow_decisions
import hr_flow_report
from tests.test_ats_public_cv import intake  # noqa: F401
from tests.test_hr_flow_decision_center import SYNTHETIC_REPORT, flow  # noqa: F401

FOUR_SECTION_REPORT = """Genel Pozisyon Uyumu: %71
Mesleki Deneyim Uyumu: %66

GÜÇLÜ YÖNLER:
- Stok sayımını planlı yaptığını somut adımlarla anlattı (Kanıt: Soru 3 — "sayımı vardiya sonunda planlı yaparım")
- WMS ekranlarında giriş-çıkış işlemlerini açıkladı (Kanıt: Soru 5 — "barkodla giriş yaparım")

ZAYIF YÖNLER:
- Pozisyonun gerektirdiği FIFO uygulamasını sorulduğunda yanlış tanımladı (Kanıt: Soru 6 — "en yeni ürün önce çıkar")

GELİŞTİRİLMESİ GEREKEN YÖNLER:
- Vardiya planlama deneyimi mentorlukla geliştirilebilir (Kanıt: Soru 8 — "planlamayı şef yapıyordu")

DİKKAT EDİLMESİ GEREKENLER:
- Forklift belgesi CV'de beyan edilmiş, mülakatta doğrulanmadı (Kanıt: CV beyanı — "forklift ehliyeti")

GENEL İK DEĞERLENDİRME ÖZETİ:
- [SENTETİK TEST VERİSİ] Depo operasyonu temel uygulamalarında kanıt var; FIFO bilgisi ve belge doğrulanmalı.
"""
NO_WEAKNESS_REPORT = FOUR_SECTION_REPORT.replace(
    "- Pozisyonun gerektirdiği FIFO uygulamasını sorulduğunda yanlış tanımladı (Kanıt: Soru 6 — \"en yeni ürün önce çıkar\")",
    hr_flow_report.NO_FINDING)


def _set_report(app_id, text):
    with closing(database.get_connection()) as db:
        token = db.cursor().execute("SELECT token FROM interview_links WHERE application_id=?", (app_id,)).fetchone()[0]
        db.cursor().execute("UPDATE interview_reports SET job_fit_analysis=? WHERE token=?", (text, token))
        db.commit()
    return token


# ---------------------------------------------------------------- 1 · AI üretimi
def test_ai_prompt_requires_four_separate_sections_and_normalizes(monkeypatch):
    from services import ai_service
    captured = {}

    class Responses:
        def create(self, model, input):
            captured["prompt"] = input
            # AI "Zayıf Yönler" başlığını unutursa: başlık açık "bulgu yok" notuyla eklenir, uydurulmaz
            return SimpleNamespace(output_text=FOUR_SECTION_REPORT.replace(
                FOUR_SECTION_REPORT[FOUR_SECTION_REPORT.index("ZAYIF YÖNLER:"):FOUR_SECTION_REPORT.index("GELİŞTİRİLMESİ")], ""))

    class Client:
        responses = Responses()

        def with_options(self, **kwargs):
            return self

    monkeypatch.setattr(ai_service, "LOCAL_PREVIEW", False)
    monkeypatch.setattr(ai_service, "client", Client())
    report = ai_service.generate_job_fit_analysis(cv_text="Sentetik CV", position="Depo Personeli",
                                                  interview_answers="Soru 1: ... Cevap: ...")
    prompt = captured["prompt"]
    order = [prompt.index(h + ":") for _, h, _ in hr_flow_report.SECTIONS]
    assert order == sorted(order)  # beş başlık, istenen sırayla
    assert "Kanıtı olmayan zayıf yön uydurma" in prompt
    assert "Eksik veya belirsiz bilgi zayıf yön DEĞİLDİR" in prompt
    assert "sağlık durumu" in prompt and "cinsiyet" in prompt and "Yaş" in prompt
    assert "(Kanıt: Soru N" in prompt and hr_flow_report.NO_FINDING in prompt
    assert "nihai karar yetkili İK personeline aittir" in prompt
    parsed = hr_flow_report.parse(report)
    assert parsed["format"] == "four_section" and parsed["missing"] == []
    assert parsed["weaknesses"] == []  # AI vermediği için boş; geliştirme maddeleri taşınmadı
    assert len(parsed["strengths"]) == 2 and len(parsed["development"]) == 1 and len(parsed["attention"]) == 1


def test_parser_keeps_sections_separate_and_reads_legacy_reports():
    parsed = hr_flow_report.parse(FOUR_SECTION_REPORT)
    assert parsed["format"] == "four_section"
    assert [len(parsed[k]) for k in ("strengths", "weaknesses", "development", "attention", "summary")] == [2, 1, 1, 1, 1]
    assert "FIFO" in parsed["weaknesses"][0] and "FIFO" not in " ".join(parsed["development"])
    legacy = hr_flow_report.parse(SYNTHETIC_REPORT)
    assert legacy["format"] == "legacy" and legacy["missing"] == ["weaknesses"]
    assert legacy["weaknesses"] == []  # eski "gelişim alanları" zayıf yön sayılmaz
    assert legacy["development"] == ["Vardiya planlama deneyimi sınırlı."]
    assert legacy["attention"] and legacy["summary"] and legacy["strengths"]
    import interview_assessments
    insufficient = hr_flow_report.parse(interview_assessments.INSUFFICIENT_EVIDENCE_REPORT)
    assert insufficient["format"] == "four_section"
    assert insufficient["strengths"] == insufficient["weaknesses"] == insufficient["development"] == []
    assert insufficient["attention"]  # eksik kanıt zayıf yön değil, doğrulanacak nokta


# ---------------------------------------------------------------- 2,4,5,6,7 · API
@pytest.mark.parametrize("intake", ["sqlite", "postgres"], indirect=True)
def test_api_returns_weaknesses_legacy_and_pending_states(flow):  # noqa: F811
    main, actor = flow["main"], flow["actor"]
    app_new, _, _ = flow["new"]("Rapor Dört", "rapor.dort@example.test")
    app_none, _, _ = flow["new"]("Rapor Zayıfsız", "rapor.zayifsiz@example.test")
    app_legacy, _, _ = flow["new"]("Rapor Eski", "rapor.eski@example.test")
    app_pending, _, _ = flow["new"]("Rapor Bekleyen", "rapor.bekleyen@example.test", complete=False)
    _set_report(app_new, FOUR_SECTION_REPORT)
    _set_report(app_none, NO_WEAKNESS_REPORT)
    report = main.flow_decision_center_detail(app_new, actor)["report"]
    assert report["ready"] and report["format"] == "four_section"
    assert report["weaknesses"] == [
        "Pozisyonun gerektirdiği FIFO uygulamasını sorulduğunda yanlış tanımladı (Kanıt: Soru 6 — \"en yeni ürün önce çıkar\")"]
    for key in ("strengths", "development", "attention", "summary"):  # mevcut alanlar korunuyor
        assert report[key]
    assert report["labels"]["weaknesses"] == "Zayıf Yönler"
    # 4 · Zayıf yön yoksa uydurma içerik yok
    none = main.flow_decision_center_detail(app_none, actor)["report"]
    assert none["weaknesses"] == [] and none["no_finding"] == "Bu alanda doğrulanmış bulgu bulunmuyor."
    # 5 · Eski üç başlıklı rapor sorunsuz açılır
    legacy = main.flow_decision_center_detail(app_legacy, actor)["report"]
    assert legacy["ready"] and legacy["format"] == "legacy" and legacy["weaknesses"] == []
    assert legacy["development"] and "Zayıf Yönler" in legacy["legacy_notice"]
    # 6 · Mülakat tamamlanmadan rapor varmış gibi gösterilmez (rapor satırı olsa bile)
    assert main.flow_decision_center_detail(app_pending, actor)["report"] is None
    with closing(database.get_connection()) as db:
        cur = db.cursor()
        token = cur.execute("SELECT token FROM interview_links WHERE application_id=?", (app_new,)).fetchone()[0]
        cur.execute("UPDATE interview_links SET status='started' WHERE token=?", (token,))
        db.commit()
    started = main.flow_decision_center_detail(app_new, actor)["report"]
    assert started["ready"] is False and started["text"] is None
    assert all(started[key] == [] for key in ("strengths", "weaknesses", "development", "attention", "summary"))
    # 7 · Başka şirketin raporuna erişim yok
    other = main.portal_access.Actor(9876, "hr_manager", flow["portal"], 55, "revision-1", "Başka Firma",
                                     products=("ats", "ai"))
    with pytest.raises(HTTPException) as error:
        main.flow_decision_center_detail(app_legacy, other)
    assert error.value.status_code == 404
    from fastapi.testclient import TestClient
    anonymous = TestClient(main.app, base_url=flow["portal"].origin)
    assert anonymous.get("/api/ik/flow/decision-center/" + app_legacy).status_code == 401
    assert hr_flow_decisions.detail(actor, app_legacy)["report"]["text"] == SYNTHETIC_REPORT
