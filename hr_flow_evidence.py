"""ERNASA HR FLOW: verifiable, tenant-bound evidence from existing records.

The UI must never equate an internal state transition with an external action.
Provider accepted is not inbox delivered, and learner progress is not an exam pass.
No interview bearer tokens, CV contents, or invitation URLs are returned.
"""
import base64
import hashlib
from contextlib import closing

from fastapi import HTTPException

from database import get_connection


def _step(code, title, state, detail, *, at=None, source=None, reference=None):
    return {"code":code, "title":title, "state":state, "detail":detail,
            "at":at, "source":source, "reference":reference}


def _reference(value):
    """A pseudonymous record pointer, never an access/bearer token."""
    return hashlib.sha256(str(value).encode()).hexdigest()[:16] if value else None


def case_evidence(company_id, case_id):
    """Read only authoritative records for the requested tenant and candidate."""
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""SELECT s.*, a.position_id, a.job_id, a.created_at AS applied_at,
            p.name AS position_name, ca.name AS candidate_name
            FROM selin_cases s
            JOIN ats_applications a ON a.id=s.application_id AND a.company_id=s.company_id
            JOIN company_positions p ON p.id=a.position_id AND p.company_id=s.company_id
            JOIN ats_candidates ca ON ca.id=a.candidate_id AND ca.company_id=s.company_id
            WHERE s.company_id=? AND s.id=?""",(company_id,case_id))
        row=c.fetchone()
        if not row:
            raise HTTPException(404,"FLOW aday dosyası bulunamadı.")
        case=dict(row)

        c.execute("""SELECT d.id,d.filename,d.size_bytes,d.created_at,b.digest,b.content_base64
            FROM ats_documents d LEFT JOIN ats_document_blobs b
             ON b.document_id=d.id AND b.company_id=d.company_id AND b.application_id=d.application_id
            WHERE d.id=? AND d.company_id=? AND d.application_id=?""",
            (case["document_id"],company_id,case["application_id"]))
        doc=c.fetchone()
        cv=None
        if doc:
            cv=dict(doc)
        doc_proven=False
        if cv and cv["digest"] and cv["content_base64"]:
            try:
                data=base64.b64decode(cv["content_base64"],validate=True)
                doc_proven=(hashlib.sha256(data).hexdigest()==cv["digest"] and
                            len(data)==cv["size_bytes"])
            except (ValueError,base64.binascii.Error):
                pass

        c.execute("""SELECT reviewed_by,reviewed_at,status,received_at,source
            FROM selin_intake_queue WHERE company_id=? AND application_id=?""",
            (company_id,case["application_id"]))
        review=c.fetchone()

        c.execute("""SELECT token,status,created_at,expires_at FROM interview_links
            WHERE token=? AND company_id=? AND application_id=?""",
            (case["interview_token"],company_id,case["application_id"]))
        interview=c.fetchone()
        email=None
        report=None
        answer_count=0
        if interview:
            c.execute("""SELECT status,initiated_at,sent_at,provider_message_id
                FROM ik_invitation_email_sends WHERE token=? AND company_id=?""",
                (interview["token"],company_id))
            email=c.fetchone()
            c.execute("""SELECT job_fit_analysis,created_at FROM interview_reports
                WHERE token=?""",(interview["token"],))
            report=c.fetchone()
            c.execute("""SELECT COUNT(*) AS n FROM interview_answers
                WHERE token=?""",(interview["token"],))
            answer_count=c.fetchone()["n"]

        c.execute("""SELECT digest,pdf_base64,archived_at FROM selin_report_archives
            WHERE company_id=? AND case_id=?""",(company_id,case_id))
        archive=c.fetchone()
        pdf_proven=False
        if archive:
            try:
                raw=base64.b64decode(archive["pdf_base64"],validate=True)
                pdf_proven=raw.startswith(b"%PDF-") and hashlib.sha256(raw).hexdigest()==archive["digest"]
            except (ValueError,base64.binascii.Error):
                pass

        c.execute("""SELECT a.program_id,a.modules_json,a.created_at,
            t.expires_at,t.revoked,p.state_json,p.updated_at AS progress_at,
            d.provider_message_id,d.accepted_at
            FROM ernasa_training_assignments a
            JOIN ernasa_training_invites t ON t.token_hash=a.token_hash
            LEFT JOIN ernasa_training_v9_progress p ON p.token_hash=a.token_hash
            LEFT JOIN flow_training_delivery d ON d.case_id=? AND d.company_id=a.company_id
            WHERE a.token_hash=? AND a.company_id=?""",
            (case_id,case["training_token_hash"],company_id))
        training=c.fetchone()

        c.execute("""SELECT event,created_at FROM selin_events
            WHERE company_id=? AND case_id=? ORDER BY created_at,id""",(company_id,case_id))
        audit=[{"event":r["event"],"at":r["created_at"]} for r in c.fetchall()]

    steps=[]
    steps.append(_step("application","ATS başvurusu","verified",
        "Aday ve pozisyon ATS kayıtlarıyla eşleşiyor.",at=case["applied_at"],
        source="ats_applications",reference=_reference(case["application_id"])))
    steps.append(_step("cv","CV dosyası","verified" if doc_proven else "unverified",
        "Arşivdeki dosyanın boyutu ve SHA-256 özeti doğrulandı." if doc_proven
        else "CV arşivi yok veya dosya bütünlüğü doğrulanamadı.",
        at=cv["created_at"] if cv else None,source="ats_document_blobs" if cv else None,
        reference=_reference(case["document_id"]) if cv else None))
    if review and review["reviewed_by"] and review["reviewed_at"]:
        steps.append(_step("approval","İK yönetici onayı","verified",
            "Yetkili kullanıcı işlemi kayıt altına almış.",at=review["reviewed_at"],
            source="selin_intake_queue",reference=_reference(review["reviewed_by"])))
    else:
        steps.append(_step("approval","İK yönetici onayı","not_recorded",
            "Bu başvuru için kuyruk üzerinden ayrı bir yönetici onayı kaydı bulunamadı."))

    steps.append(_step("interview","Mülakat bağlantısı","verified" if interview else "not_recorded",
        ("Mülakat oluşturuldu. Son durum: "+str(interview["status"])+
         "; son geçerlilik: "+str(interview["expires_at"] or "bilinmiyor"))
        if interview else "İlgili firma ve başvuruyla eşleşen mülakat yok.",
        at=interview["created_at"] if interview else None,
        source="interview_links" if interview else None,
        reference=_reference(interview["token"]) if interview else None))
    accepted=bool(email and email["status"]=="sent" and email["provider_message_id"])
    steps.append(_step("invite_email","Mülakat e-postası",
        "provider_accepted" if accepted else
        "failed" if email and email["status"]=="failed" else
        "pending" if email and email["status"]=="pending" else "unverified",
        "E-posta servisinden kabul kimliği kaydedildi; alıcıya teslim edildiği henüz kanıtlanmadı."
        if accepted else "E-posta servisi kabul kanıtı bulunmuyor; gönderildi denilemez.",
        at=email["sent_at"] or email["initiated_at"] if email else None,
        source="ik_invitation_email_sends" if email else None,
        reference=email["provider_message_id"] if accepted else None))

    completed=bool(interview and interview["status"]=="completed")
    steps.append(_step("answers","Mülakat yanıtları",
        "verified" if completed and answer_count>0 else "pending",
        str(answer_count)+" kayıtlı yanıt; "+("mülakat tamamlandı." if completed else "mülakat tamamlanmadı."),
        source="interview_answers" if interview else None))
    steps.append(_step("assessment","AI mülakat raporu",
        "verified" if completed and report and report["job_fit_analysis"] else "pending",
        "Tamamlanan mülakata ait rapor kaydı bulundu." if completed and report and report["job_fit_analysis"]
        else "Mülakat değerlendirmesine ait doğrulanmış rapor bulunmuyor.",
        at=report["created_at"] if report and "created_at" in report.keys() else None,
        source="interview_reports" if report else None))
    steps.append(_step("pdf","PDF rapor arşivi",
        "verified" if pdf_proven else "unverified",
        "PDF içerik imzası ve SHA-256 bütünlük özeti eşleşiyor." if pdf_proven
        else "PDF arşivi yok ya da bütünlük kontrolü başarısız.",
        at=archive["archived_at"] if archive else None,
        source="selin_report_archives" if archive else None,
        reference=archive["digest"] if pdf_proven else None))

    if training:
        import json
        modules=json.loads(training["modules_json"])
        progress=json.loads(training["state_json"]) if training["state_json"] else {}
        done=progress.get("done",{})
        completed_count=sum(done.get(m) is True for m in modules)
        steps.append(_step("training_assignment","Eğitim ataması","verified",
            str(len(modules))+" modül kayda alınmış. Bu, davetin gönderildiğini göstermez.",
            at=training["created_at"],source="ernasa_training_assignments",
            reference=_reference(case["training_token_hash"])))
        training_accepted=bool(training["provider_message_id"] and training["accepted_at"])
        steps.append(_step("training_email","Eğitim e-postası",
            "provider_accepted" if training_accepted else "unverified",
            "E-posta servisi isteği kabul etti; alıcıya ulaştığı kanıtlanmış değil."
            if training_accepted else "Eğitim davetinin e-posta sağlayıcısınca kabul edildiğine ilişkin kayıt yok.",
            at=training["accepted_at"],source="flow_training_delivery" if training_accepted else None,
            reference=training["provider_message_id"] if training_accepted else None))
        steps.append(_step("training_progress","Ders ilerlemesi",
            "self_reported" if training["state_json"] else "pending",
            str(completed_count)+"/"+str(len(modules))+
            " modül tamamlanmış görünüyor. Bu veri sınav başarısı değildir.",
            at=training["progress_at"],source="ernasa_training_v9_progress" if training["state_json"] else None))
    else:
        steps.append(_step("training_assignment","Eğitim ataması","not_recorded",
                           "Adaya bağlı eğitim atama kaydı henüz bulunmuyor."))
        steps.append(_step("training_email","Eğitim e-postası","not_recorded",
                           "Eğitim daveti kaydı bulunmuyor."))
        steps.append(_step("training_progress","Ders ilerlemesi","not_recorded",
                           "Eğitim ilerlemesi henüz yok."))

    return {"product":"ERNASA HR FLOW","case_id":case_id,"status":case["status"],
            "candidate":case["candidate_name"],"position":case["position_name"],
            "error_code":case["error_code"],
            "steps":steps,"audit":audit,
            "notice":"Kayıt oluştu, servis isteği kabul edildi, alıcıya teslim ve eğitim başarısı farklı durumlardır. İşe alım kararı yalnızca yetkili insana aittir."}
