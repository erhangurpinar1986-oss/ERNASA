"""ERNASA HR FLOW: one-click, evidence-based ATS pre-screen and human approval.

No CV is automatically rejected. Scores are meaningful only with explicitly
configured job criteria; missing evidence never becomes a fabricated zero.
All queries are tenant/role scoped. Sending email or minting interview links is
out of scope for screening and permission requests.
"""
import hashlib
import json
from contextlib import closing
from datetime import datetime,timezone
from uuid import uuid4

from fastapi import HTTPException

import ats
import ats_matching
import recruitment
from database import get_connection
from recruitment import write


def now():
    return datetime.now(timezone.utc).isoformat()


def initialize_schema():
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS flow_screening_results (
            company_id BIGINT NOT NULL, application_id TEXT NOT NULL,
            position_id TEXT NOT NULL, document_id TEXT NOT NULL,
            cv_digest TEXT NOT NULL, criteria_digest TEXT NOT NULL,
            analysis_state TEXT NOT NULL, match_score REAL, coverage REAL,
            details_json TEXT NOT NULL, analyzed_at TEXT NOT NULL,
            PRIMARY KEY(company_id,application_id,document_id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS flow_interview_permissions (
            id TEXT PRIMARY KEY, company_id BIGINT NOT NULL,
            application_id TEXT NOT NULL, document_id TEXT NOT NULL,
            requested_by BIGINT NOT NULL, requested_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','approved','declined')),
            decided_by BIGINT, decided_at TEXT, decision_note TEXT,
            UNIQUE(company_id,application_id)
        )""")
        c.execute("""CREATE INDEX IF NOT EXISTS flow_interview_permissions_scope
            ON flow_interview_permissions(company_id,status,requested_at)""")
        db.commit()


def _job_criteria(c,tenant,position_id,actor=None):
    # Match the Position menu's permission model. Staff cannot discover
    # another department's vacancy or the screening criteria attached to it.
    if actor is not None and actor.role == "hr_staff":
        c.execute("""SELECT 1 FROM ats_position_members
            WHERE company_id=? AND position_id=? AND user_id=?""",
            (tenant,position_id,actor.user_id))
        if not c.fetchone():
            raise HTTPException(404,"Pozisyon bulunamadı.")
    c.execute("""SELECT p.id,p.name,f.fields FROM company_positions p
        LEFT JOIN ats_position_profiles f ON f.position_id=p.id AND f.company_id=p.company_id
        WHERE p.company_id=? AND p.id=?""",(tenant,position_id))
    row=c.fetchone()
    if not row:
        raise HTTPException(404,"Pozisyon bulunamadı.")
    try:
        fields=json.loads(row["fields"] or "{}")
    except (ValueError,TypeError):
        fields={}
    criteria=str(fields.get("matching_criteria") or "")
    digest=hashlib.sha256(criteria.encode("utf-8")).hexdigest()
    return row["name"],criteria,digest


def _result_row(c,tenant,application_id,document_id):
    c.execute("""SELECT * FROM flow_screening_results
        WHERE company_id=? AND application_id=? AND document_id=?""",
        (tenant,application_id,document_id))
    row=c.fetchone()
    return dict(row) if row else None


def _permission(c,tenant,application_id):
    c.execute("""SELECT id,status,requested_at,decided_at FROM flow_interview_permissions
        WHERE company_id=? AND application_id=?""",(tenant,application_id))
    row=c.fetchone()
    return dict(row) if row else None


def screen_position(actor,position_id,cv_root,limit=20,offset=0):
    """One action analyzes only the HR user's accessible applications.

    Paginate 20 per batch and reuse matching results when document AND
    job criteria have not changed; 10MB document limit enforced by ATS.
    """
    if "ats" not in actor.products or not actor.company_id:
        raise HTTPException(403,"ATS yetkisi gerekli.")
    if limit!=20 or not 0<=offset<=10000 or offset%20:
        raise HTTPException(422,"Geçersiz sayfalama.")
    with closing(get_connection()) as db:
        c=db.cursor()
        position,criteria,criteria_digest=_job_criteria(c,actor.company_id,position_id,actor)
    rows=ats.list_applications(actor,position_id=position_id,limit=20,offset=offset)
    output=[]
    for applicant in rows["applications"]:
        app_id=applicant["id"]
        with closing(get_connection()) as db:
            c=db.cursor()
            c.execute("""SELECT d.id,d.filename,dg.digest FROM ats_documents d
                LEFT JOIN ats_document_digests dg ON dg.company_id=d.company_id
                 AND dg.application_id=d.application_id AND dg.document_id=d.id
                WHERE d.company_id=? AND d.application_id=?
                ORDER BY d.created_at DESC,d.id DESC LIMIT 1""",
                (actor.company_id,app_id))
            doc=c.fetchone()
            doc=dict(doc) if doc else None
            existing=_result_row(c,actor.company_id,app_id,doc["id"]) if doc else None
            permission=_permission(c,actor.company_id,app_id)
        record={"application_id":app_id,"candidate":applicant["name"],
                "position":position,"source":applicant["source"],
                "has_cv":bool(doc),"permission":permission,
                "analysis_state":"cv_missing","match_score":None,
                "coverage":None,"evidence":[],"note":"CV dosyası eklenmemiş."}
        if not doc:
            output.append(record)
            continue
        if not doc["digest"]:
            record.update(analysis_state="document_unverified",
                          note="Belge bütünlük kanıtı yok; manuel inceleyin.")
            output.append(record)
            continue
        if existing and existing["cv_digest"]==doc["digest"] and existing["criteria_digest"]==criteria_digest:
            data=json.loads(existing["details_json"])
        else:
            try:
                data=ats_matching.evidence_match(cv_root,actor,app_id,doc["id"])
            except HTTPException as exc:
                # Access restriction and corrupted archives are not hidden.
                if exc.status_code==403:
                    raise
                data={"match_score":None,"evidence_coverage":None,
                      "requirements":[],
                      "reason":"Belge analizi başarısız; manuel kontrol gerekiyor."}
            except (OSError,ValueError,RuntimeError):
                data={"match_score":None,"evidence_coverage":None,
                      "requirements":[],"reason":"CV incelenemedi; manuel kontrol gerekiyor."}
            if not criteria:
                state="criteria_missing"
            elif data.get("match_score") is None:
                state="review_required"
            else:
                state="analyzed"
            # Save observed analysis, never invent absent scores.
            with write(actor.company_id) as c:
                c.execute("""INSERT INTO flow_screening_results
                    (company_id,application_id,position_id,document_id,cv_digest,
                     criteria_digest,analysis_state,match_score,coverage,details_json,analyzed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(company_id,application_id,document_id) DO UPDATE SET
                        cv_digest=excluded.cv_digest,criteria_digest=excluded.criteria_digest,
                        analysis_state=excluded.analysis_state,match_score=excluded.match_score,
                        coverage=excluded.coverage,details_json=excluded.details_json,
                        analyzed_at=excluded.analyzed_at""",
                    (actor.company_id,app_id,position_id,doc["id"],doc["digest"],
                     criteria_digest,state,data.get("match_score"),
                     data.get("evidence_coverage"),
                     json.dumps(data,ensure_ascii=False,default=str),now()))
            existing={"analysis_state":state}
        record.update(document_id=doc["id"],
                      analysis_state=existing["analysis_state"],
                      match_score=data.get("match_score"),
                      coverage=data.get("evidence_coverage"),
                      evidence=[{"requirement":i.get("requirement"),"status":i.get("status")}
                                for i in data.get("requirements",[])][:15],
                      note=data.get("reason") or "ATS karşılaştırması işlendi; nihai karar İK'ya aittir.")
        output.append(record)
    return {"position":position,"total":rows["total"],"limit":20,"offset":offset,
            "candidates":output,
            "note":"Puan yalnızca kanıt bulunan kriterlerden hesaplanır. Kapsam düşükse yanıltıcı olabilir; otomatik ret uygulanmaz."}


def request_permission(actor,application_id,document_id):
    """HR staff proposes an interview; never sends or schedules an invite."""
    if "ats" not in actor.products or actor.user_id is None:
        raise HTTPException(403,"ATS İK yetkisi gerekiyor.")
    with write(actor.company_id) as c:
        app=ats.application(c,actor,application_id)
        if app["closure"]:
            raise HTTPException(409,"Kapanmış başvuruda mülakat izni istenemez.")
        c.execute("""SELECT 1 FROM ats_documents
            WHERE company_id=? AND application_id=? AND id=?""",
            (actor.company_id,application_id,document_id))
        if not c.fetchone():
            raise HTTPException(404,"Bu adaya ait CV bulunamadı.")
        # A successful or pending screening record is required before request.
        row=_result_row(c,actor.company_id,application_id,document_id)
        if not row or row["analysis_state"]!="analyzed" or row["match_score"] is None:
            raise HTTPException(409,"Pozisyona ait doğrulanabilir ATS analizi olmadan izin talebi açılamaz.")
        if row["position_id"]!=app["position_id"]:
            raise HTTPException(409,"Analiz farklı pozisyona ait; yeniden değerlendirin.")
        c.execute("""SELECT digest FROM ats_document_digests
            WHERE company_id=? AND application_id=? AND document_id=?""",
            (actor.company_id,application_id,document_id))
        digest=c.fetchone()
        if not digest or digest["digest"]!=row["cv_digest"]:
            raise HTTPException(409,"CV değişmiş; ATS analizini yenileyin.")
        _,_,current_criteria=_job_criteria(c,actor.company_id,app["position_id"],actor)
        if current_criteria!=row["criteria_digest"]:
            raise HTTPException(409,"Pozisyon kriterleri değişmiş; ATS analizini yenileyin.")
        previous=_permission(c,actor.company_id,application_id)
        if previous:
            return previous
        request_id=uuid4().hex
        when=now()
        c.execute("""INSERT INTO flow_interview_permissions
            (id,company_id,application_id,document_id,requested_by,requested_at,status)
            VALUES(?,?,?,?,?,?,'pending')""",
            (request_id,actor.company_id,application_id,document_id,actor.user_id,when))
    return {"id":request_id,"status":"pending","requested_at":when,"decided_at":None}


def decide_permission(actor,request_id,decision):
    """Manager approves the HR request, NOT an unscheduled email or hiring."""
    if actor.role not in recruitment.MANAGERS or "ats" not in actor.products:
        raise HTTPException(403,"İK yönetici yetkisi gerekli.")
    if decision not in ("approved","declined"):
        raise HTTPException(422,"Geçersiz karar.")
    with write(actor.company_id) as c:
        c.execute("""SELECT * FROM flow_interview_permissions
            WHERE id=? AND company_id=?""",(request_id,actor.company_id))
        row=c.fetchone()
        if not row:
            raise HTTPException(404,"İzin talebi bulunamadı.")
        if row["status"]!="pending":
            raise HTTPException(409,"Talep daha önce sonuçlandırıldı.")
        c.execute("""UPDATE flow_interview_permissions
            SET status=?,decided_by=?,decided_at=?
            WHERE id=? AND company_id=? AND status='pending'""",
            (decision,actor.user_id,now(),request_id,actor.company_id))
        if c.rowcount!=1:
            raise HTTPException(409,"Talep eşzamanlı sonuçlandırıldı.")
    return {"id":request_id,"status":decision,
            "notice":"Bu karar adaya davet göndermez. Aday bilgilendirmesi ve müsaitlik teyidi ayrıca gereklidir."}
