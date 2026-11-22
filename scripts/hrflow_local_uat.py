"""ERNASA HR FLOW — yerel kullanıcı kabul testi (UAT) başlatıcısı.

YALNIZCA YEREL TEST İÇİNDİR. Üretim kodunu değiştirmez; aynı süreçte şu test
yerine geçirmelerini yapar:
  * Firma portalı 127.0.0.1 adresinde "sentetik-firma" olarak çözülür.
  * CONTROL (firma doğrulama) servisi yerine sentetik firma kimliği kullanılır.
  * Tüm e-postalar dışarı GÖNDERİLMEZ; ``uat_data/posta`` klasörüne yazılır.
  * Ayrı bir SQLite test veritabanı (``uat_data/ernasa_uat.db``) kullanılır.
  * OpenAI çağrıları yerel sentetik yanıtlarla değiştirilir.

Güvenlik: DATABASE_URL, RENDER veya e-posta sağlayıcı anahtarları tanımlıysa
başlatıcı çalışmayı reddeder (üretim kaynağına bağlanma riski).
"""
import argparse
import html
import json
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("ERNASA_UAT_DIR", ROOT / "uat_data")).resolve()
PORT = int(os.getenv("ERNASA_UAT_PORT", "8151"))
ORIGIN = f"http://127.0.0.1:{PORT}"
COMPANY_ID, MANAGER_ID = 1234, 21
FORBIDDEN = ("DATABASE_URL", "RENDER", "ERNASA_BREVO_API_KEY", "ERNASA_RESEND_API_KEY", "BREVO_API_KEY",
             "RESEND_API_KEY", "ERNASA_CONTROL_DATABASE_URL", "ERNASA_CONTROL_PUBLIC_ORIGIN",
             "ERNASA_CONTROL_DB_PATH", "ERNASA_IK_CONTROL_KEY", "OPENAI_API_KEY")
REPORT = """Genel Pozisyon Uyumu: %74
Mesleki Deneyim Uyumu: %70
GÜÇLÜ YÖNLER:
- Depo düzeni ve stok sayımı konusunda somut örnekler verdi.
GELİŞTİRİLMESİ GEREKEN ALANLAR:
- Vardiya planlama deneyimi sınırlı.
DİKKAT EDİLMESİ GEREKEN NOKTALAR:
- Forklift belgesi CV'de var; mülakatta doğrulanmadı.
İK DEĞERLENDİRME ÖZETİ:
- SENTETİK UAT RAPORU. Nihai karar yetkili İK personeline aittir.
"""


def guard():
    present = [name for name in FORBIDDEN if os.getenv(name)]
    if present:
        sys.exit("GÜVENLİK: Şu ortam değişkenleri tanımlı olduğu için yerel test başlatılmadı: "
                 + ", ".join(present) + ". Bu değişkenleri kaldırıp yeni bir komut penceresinde deneyin.")


def configure_environment():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "posta").mkdir(exist_ok=True)
    (DATA / "uploads").mkdir(exist_ok=True)
    key_file = DATA / "tracking.key"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    os.environ.update({
        "ERNASA_IK_LOCAL_PREVIEW": "1",
        "ERNASA_EMAIL_SANDBOX_DIR": str(DATA / "posta"),
        "ERNASA_FLOW_TRACKING_KEY": key_file.read_text(encoding="utf-8").strip(),
        "ERNASA_PUBLIC_CV_ENABLED": "1",
        "ERNASA_PUBLIC_CV_RATE_SECRET": "uat-only-rate-secret-" + "0" * 24,
        "ERNASA_IK_CONTROL_URL": "http://127.0.0.1:1",  # hiçbir zaman çağrılmaz; aşağıda yerine geçirilir
    })
    sys.path.insert(0, str(ROOT))


def patch_runtime():
    import control_integration as control
    import database
    database.DATABASE_URL = None
    database.DATABASE_PATH = DATA / "ernasa_uat.db"
    portal = control.Portal("sentetik-firma", ORIGIN)
    identity = {"company_id": COMPANY_ID, "role": "hr_manager", "version": "uat-1",
                "company_name": "Sentetik Lojistik A.Ş.", "user_name": "Sentetik İK Yöneticisi",
                "products": ["ats", "ai"], "interview_quota": None, "permissions": [],
                "month_bounds": ["2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00"]}

    def portal_for(request):
        host = request.headers.get("host", "").lower()
        return portal if host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}") else None

    def control_call(_portal, operation, **fields):
        if operation == "session":
            return dict(identity)
        if operation == "resolve":
            return {"company_id": COMPANY_ID, "name": identity["company_name"], "products": identity["products"]}
        if operation == "assignees":
            return {"users": [{"id": MANAGER_ID, "name": identity["user_name"]}]}
        return {}

    control.portal_for = portal_for
    control.control_call = control_call
    database.initialize_database()
    from backend import main
    main.UPLOAD_DIRECTORY = DATA / "uploads"
    main.generate_job_fit_analysis = lambda *args, **kwargs: REPORT
    main.generate_interview_question = lambda **kwargs: (
        f"Sentetik mülakat sorusu #{kwargs.get('question_number')}: Bu konudaki deneyiminizi örnekle anlatır mısınız?")
    import interview_assessments
    for name in ("generate_job_fit_analysis",):
        if hasattr(interview_assessments, name):
            setattr(interview_assessments, name, lambda *args, **kwargs: REPORT)
    return main, portal, identity


def seed(main, portal):
    """Sentetik firma, pozisyon, kriter, yayınlanmış ilan ve açık otomasyon (bir kez)."""
    import ats
    import ats_public_cv
    import hr_flow_automation
    import hr_flow_contacts
    import hr_flow_decisions
    import hr_flow_screening
    import recruitment
    from contextlib import closing
    import database
    ats_public_cv.initialize_schema()
    hr_flow_contacts.initialize_schema()
    hr_flow_screening.initialize_schema()
    hr_flow_decisions.initialize_schema()
    hr_flow_automation.initialize_schema()
    marker = DATA / "seed.json"
    if marker.exists():
        return json.loads(marker.read_text(encoding="utf-8"))
    manager = SimpleNamespace(company_id=COMPANY_ID, user_id=MANAGER_ID, role="hr_manager", products=("ats",))
    dept = recruitment.add_department(manager, "Lojistik")
    position = recruitment.save_position(manager, recruitment.Position(name="Depo Personeli", department_id=dept["id"]))
    job = ats.save_job(manager, ats.Job(position_id=position["id"], title="Depo Personeli (Sentetik İlan)",
                                        summary="Sentetik UAT ilanı: depo operasyonu, stok sayımı, WMS.",
                                        state="published", confirm_publish=True,
                                        notice="Aydınlatma metni (sentetik): Başvurunuz ve CV'niz değerlendirilecektir.",
                                        notice_version="v1"))
    with closing(database.get_connection()) as db:
        cursor = db.cursor()
        row = cursor.execute("SELECT fields FROM ats_position_profiles WHERE position_id=?", (position["id"],)).fetchone()
        fields = json.loads(row["fields"] or "{}") if row else {}
        fields["matching_criteria"] = "Yetkinlik | Depo deneyimi | 60 | depo\nYetkinlik | WMS kullanımı | 40 | wms"
        cursor.execute("UPDATE ats_position_profiles SET fields=? WHERE position_id=?", (json.dumps(fields), position["id"]))
        db.commit()
    actor = main.portal_access.Actor(COMPANY_ID, "hr_manager", portal, MANAGER_ID, "uat-1", "Sentetik Lojistik A.Ş.",
                                     products=("ats", "ai"))
    hr_flow_automation.configure(actor, position["id"], hr_flow_automation.AutomationConfig(
        enabled=True, slot_weekdays=[1, 2, 3, 4, 5, 6, 7], slot_start="08:00", slot_end="22:00", slot_minutes=30))
    info = {"job_id": job["id"], "position_id": position["id"]}
    marker.write_text(json.dumps(info), encoding="utf-8")
    return info


def synthetic_pdf(name, extra):
    import pymupdf
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), f"{name} - Sentetik CV. {extra}")
    data = doc.tobytes()
    doc.close()
    return data


def add_uat_routes(main, portal, info):
    from fastapi import Form, Request
    from fastapi.responses import HTMLResponse, RedirectResponse
    from contextlib import closing
    import database
    import ats_public_cv
    from pydantic import BaseModel

    style = ("body{font:15px/1.5 system-ui,Segoe UI,sans-serif;max-width:980px;margin:0 auto;padding:20px;color:#1d2733}"
             "h1{color:#0b2e73}.card{border:1px solid #d9e1ea;border-radius:10px;padding:12px 16px;margin:12px 0;background:#fff}"
             ".warn{background:#fff7e6;border-color:#f2c94c}a.btn,button{display:inline-block;background:#0b2e73;color:#fff;"
             "padding:8px 14px;border-radius:8px;text-decoration:none;border:0;font:inherit;cursor:pointer;margin:4px 4px 4px 0}"
             "pre{white-space:pre-wrap;background:#f6f8fb;padding:10px;border-radius:8px}table{border-collapse:collapse;width:100%}"
             "td,th{border-bottom:1px solid #e5e9f0;padding:6px;text-align:left;vertical-align:top}")

    def page(title, body):
        return HTMLResponse(f'<!doctype html><html lang="tr"><meta charset="utf-8"><meta name="viewport" '
                            f'content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
                            f'<style>{style}</style><main><h1>{html.escape(title)}</h1>{body}</main></html>')

    # Ön yüz firma portalı modunu alan adından anlar (*.ernasa.com). Yerel testte hosts dosyası
    # düzenlemeden 127.0.0.1 için de portal modunu açmak üzere yalnız bu süreçte JS yanıtı uyarlanır.
    from fastapi.responses import Response
    portal_js = ("ernasa_v5_ik.js", "script.js", "aday-takip.js")

    @main.app.middleware("http")
    async def uat_portal_hostname(request: Request, call_next):
        name = request.url.path.rsplit("/", 1)[-1]
        if request.url.path.startswith("/static/") and name in portal_js:
            source = (main.FRONTEND_DIR / name).read_text(encoding="utf-8")
            source = source.replace('location.hostname.endsWith(".ernasa.com")',
                                    '(location.hostname.endsWith(".ernasa.com") || location.host === "127.0.0.1:%d")' % PORT)
            return Response(source, media_type="application/javascript",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return await call_next(request)

    @main.app.get("/uat", include_in_schema=False)
    def uat_home():
        return page("ERNASA HR FLOW · Yerel Kabul Testi", f"""
<div class="card warn"><b>İzole test ortamı.</b> Sentetik firma ve adaylar. E-postalar dışarı gönderilmez; aşağıdaki
"Test posta kutusu"na yazılır. Canlı veritabanı veya Render kullanılmaz.</div>
<div class="card"><h2>1 · İK paneli</h2><a class="btn" href="/uat/giris">İK yöneticisi olarak giriş yap</a>
<p>ERNASA HR FLOW menüsünde üç panelli İK Karar Merkezi, "Otomatik süreç ayarları" ve "Raporlar" bölümleri bulunur.</p></div>
<div class="card"><h2>2 · Aday başvurusu (ilan bağlantısı)</h2>
<a class="btn" href="/basvuru/{info['job_id']}">Sentetik ilana başvur</a>
<p>CV olarak herhangi bir PDF/DOCX yükleyebilirsiniz. E-posta için <code>@example.test</code> uzantılı adres kullanın.</p>
<form method="post" action="/uat/hizli-aday"><button type="submit">Hazır sentetik aday başvurusu oluştur</button></form></div>
<div class="card"><h2>3 · Test posta kutusu</h2><a class="btn" href="/uat/posta">Adaya giden e-postaları gör</a>
<p>Gün/saat seçimi, mülakat ve takip bağlantıları bu e-postalardadır.</p></div>
<div class="card"><h2>4 · Zaman simülasyonu (yalnız test)</h2><a class="btn" href="/uat/zaman">Randevuları yönet</a>
<p>Seçilen randevu saatini beklemeden 8 saatlik pencereyi açmak veya süresini doldurmak için kullanılır.</p></div>""")

    @main.app.get("/uat/giris", include_in_schema=False)
    def uat_login():
        token = main.portal_access.new_session(portal, {"company_id": COMPANY_ID, "user_id": MANAGER_ID, "version": "uat-1"})
        response = RedirectResponse("/ik-v5", status_code=303)
        response.set_cookie(main.portal_access.COOKIE, token, httponly=True, samesite="lax", path="/")
        return response

    @main.app.post("/uat/hizli-aday", include_in_schema=False)
    def uat_fast_candidate():
        stamp = datetime.now().strftime("%H%M%S")
        name = f"Sentetik Aday {stamp}"
        fields = SimpleNamespace(name=name, email=f"aday{stamp}@example.test", phone="", acknowledge=True,
                                 notice_version="v1")
        import ats
        application = ats.PublicApplication(name=name, email=fields.email, phone="", acknowledge=True,
                                            notice_version="v1")
        ats_public_cv.submit(COMPANY_ID, info["job_id"], application, "sentetik_cv.pdf",
                             synthetic_pdf(name, "Lojistik depo, stok sayımı ve WMS deneyimi."), "direct", "127.0.0.1")
        return RedirectResponse("/uat/posta", status_code=303)

    @main.app.get("/uat/posta", include_in_schema=False)
    def uat_mailbox():
        rows = []
        for path in sorted((DATA / "posta").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
            mail = json.loads(path.read_text(encoding="utf-8"))
            body = html.escape(mail["body"])
            body = re.sub(r"(https?://[^\s<]+)", lambda m: f'<a href="{m[1]}">{m[1]}</a>', body)
            when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%d.%m.%Y %H:%M:%S")
            rows.append(f'<div class="card"><b>{html.escape(mail["subject"])}</b><br>Alıcı: {html.escape(mail["to"])} · '
                        f'{when}<pre>{body}</pre></div>')
        return page("Test posta kutusu", '<p><a href="/uat">← UAT ana sayfa</a> · Sayfayı yenileyerek yeni '
                    'e-postaları görün (otomatik işlemler birkaç saniye sürer).</p>' + ("".join(rows) or "<p>Henüz e-posta yok.</p>"))

    @main.app.get("/uat/zaman", include_in_schema=False)
    def uat_time():
        with closing(database.get_connection()) as db:
            rows = db.cursor().execute("""SELECT s.id,s.starts_at,s.status,c.name FROM ats_schedules s
                JOIN ats_applications a ON a.id=s.application_id JOIN ats_candidates c ON c.id=a.candidate_id
                WHERE s.status='planned' ORDER BY s.starts_at""").fetchall()
        items = "".join(
            f'<tr><td>{html.escape(r["name"])}</td><td>{html.escape(r["starts_at"])}</td><td>'
            f'<form method="post" style="display:inline"><input type="hidden" name="schedule" value="{r["id"]}">'
            f'<button name="mode" value="open">Pencereyi şimdi aç</button>'
            f'<button name="mode" value="expire">8 saati doldur</button></form></td></tr>' for r in rows)
        return page("Zaman simülasyonu (yalnız test)", '<p><a href="/uat">← UAT ana sayfa</a></p>'
                    '<p>Bu işlem yalnızca yerel test veritabanında randevu başlangıcını kaydırır.</p>'
                    f'<table><tr><th>Aday</th><th>Randevu (UTC)</th><th>İşlem</th></tr>{items}</table>')

    @main.app.post("/uat/zaman", include_in_schema=False)
    def uat_time_shift(schedule: str = Form(...), mode: str = Form(...)):
        now = datetime.now(timezone.utc)
        start = now - (timedelta(hours=8, minutes=1) if mode == "expire" else timedelta(minutes=1))
        with closing(database.get_connection()) as db:
            cur = db.cursor()
            cur.execute("UPDATE ats_schedules SET starts_at=?,ends_at=? WHERE id=? AND status='planned'",
                        (start.isoformat(), (start + timedelta(minutes=30)).isoformat(), schedule))
            cur.execute("""UPDATE interview_links SET expires_at=? WHERE status IN ('waiting','created','opened') AND token IN
                (SELECT token FROM flow_scheduled_interviews WHERE schedule_id=?)""",
                        ((start + timedelta(hours=8)).isoformat(), schedule))
            db.commit()
        return RedirectResponse("/uat/zaman", status_code=303)


def main_entry():
    parser = argparse.ArgumentParser(description="ERNASA HR FLOW yerel kabul testi")
    parser.add_argument("--check", action="store_true", help="Yalnızca kurulum doğrulaması yap ve çık")
    args = parser.parse_args()
    guard()
    configure_environment()
    main, portal, _ = patch_runtime()
    info = seed(main, portal)
    add_uat_routes(main, portal, info)
    if args.check:
        print("ERNASA UAT kurulumu hazır:", json.dumps(info))
        return
    import uvicorn
    print(f"\nERNASA HR FLOW yerel test: {ORIGIN}/uat  (kapatmak için bu pencerede Ctrl+C)\n")
    uvicorn.run(main.app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main_entry()
