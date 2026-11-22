"""Durable AI assessment jobs for completed company interviews."""
import asyncio
import logging
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException

from ats_matching import relevant_text
from database import get_connection
from recruitment import record, write


logger = logging.getLogger(__name__)


def enqueue(cursor, tenant, token):
    cursor.execute("""INSERT INTO ik_interview_assessments(token,company_id,status,updated_at)
        SELECT ?,?,'queued',? WHERE NOT EXISTS (SELECT 1 FROM ik_partner_interviews WHERE token=?)
        AND NOT EXISTS (SELECT 1 FROM interview_reports WHERE token=?)
        ON CONFLICT(token) DO NOTHING""", (token,tenant,datetime.now(timezone.utc).isoformat(),token,token))


def status(cursor, tenant, token, has_report=False):
    if has_report:
        return 'completed'
    cursor.execute('SELECT status FROM ik_interview_assessments WHERE token=? AND company_id=?',(token,tenant))
    row = cursor.fetchone()
    return row['status'] if row else 'not_started'


def retry(actor, token):
    tenant = actor.company_id
    with write(tenant) as cursor:
        cursor.execute("""SELECT i.status, p.token AS partner, r.token AS report
            FROM interview_links i LEFT JOIN ik_partner_interviews p ON p.token=i.token AND p.company_id=i.company_id
            LEFT JOIN interview_reports r ON r.token=i.token
            WHERE i.token=? AND i.company_id=? AND i.status IS DISTINCT FROM 'new'""", (token,tenant))
        interview = cursor.fetchone()
        if not interview:
            raise HTTPException(404,'Mülakat bulunamadı.')
        if interview['status'] != 'completed' or interview['partner']:
            raise HTTPException(409,'Bu mülakatta değerlendirme bu ekrandan başlatılamaz.')
        previous = status(cursor,tenant,token,bool(interview['report']))
        if previous == 'not_started':
            enqueue(cursor,tenant,token)
        elif previous == 'failed':
            cursor.execute("""UPDATE ik_interview_assessments SET status='queued',updated_at=?,
                claim_id=NULL,lease_until=NULL WHERE company_id=? AND token=?""",
                (datetime.now(timezone.utc).isoformat(),tenant,token))
        else:
            return previous
        record(cursor,actor,'interview_assessment_requested',token)
        return 'queued'


def claim():
    now = datetime.now(timezone.utc)
    available = "(status='queued' OR (status='running' AND lease_until<=?))"
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute(f"""SELECT token,company_id FROM ik_interview_assessments WHERE {available}
            ORDER BY updated_at,token LIMIT 1""",(now.isoformat(),))
        row = cursor.fetchone()
    if not row:
        return None
    token, tenant, claim_id = row['token'], row['company_id'], uuid4().hex
    with write(tenant) as cursor:
        cursor.execute(f"""UPDATE ik_interview_assessments SET status='running',claim_id=?,lease_until=?,updated_at=?
            WHERE token=? AND company_id=? AND {available}""",
            (claim_id,(now+timedelta(minutes=3)).isoformat(),now.isoformat(),token,tenant,now.isoformat()))
        return (token,tenant,claim_id) if cursor.rowcount else None


INSUFFICIENT_EVIDENCE_REPORT = """MÜLAKAT SONUCU: DEĞERLENDİRİLEMEDİ — YETERLİ MÜLAKAT KANITI YOK

Genel Pozisyon Uyumu: Değerlendirilemedi
Mesleki Deneyim Uyumu: Değerlendirilemedi
Mesleki Bilgi / Uygulama: Değerlendirilemedi
Problem Çözme: Değerlendirilemedi
İletişim: Değerlendirilemedi
Takım Çalışması: Değerlendirilemedi

GÜÇLÜ YÖNLER:
Bu alanda doğrulanmış bulgu bulunmuyor.

ZAYIF YÖNLER:
Bu alanda doğrulanmış bulgu bulunmuyor.

GELİŞTİRİLMESİ GEREKEN YÖNLER:
Bu alanda doğrulanmış bulgu bulunmuyor.

DİKKAT EDİLMESİ GEREKENLER:
- Mülakat cevapları anlamlı bir değerlendirmeyi desteklemiyor; yetkinlik eksikliği çıkarımı yapılmadı.
- CV beyanları mülakat kanıtı olarak kullanılmadı.

GENEL İK DEĞERLENDİRME ÖZETİ:
Otomatik kabul veya ret kararı verilmedi. İK incelemesi ve gerekirse yeniden mülakat önerilir.
"""


def insufficient_answer_evidence(answers):
    """Fail closed on unmistakably unusable transcripts; never assign invented scores.

    This is a conservative preflight, not a semantic assessment or a hiring decision.
    Short legitimate answers can be flagged for human review, never auto-rejected.
    """
    texts = [str(row['answer'] or '').strip() for row in answers.values()]
    words = [re.findall(r"[^\W_]+", value, flags=re.UNICODE) for value in texts]
    total_words = sum(len(items) for items in words)
    nonempty = sum(bool(value) for value in texts)
    long_unbroken = sum(
        bool(re.search(r"[^\W_]{24,}", value, flags=re.UNICODE))
        for value in texts
    )
    if nonempty < 5 or total_words < 20:
        return True
    if long_unbroken >= 5:
        return True
    return False


def evaluate(token, tenant, generate):
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT cv_text,position FROM interview_links WHERE token=? AND company_id=? AND status='completed'",
                       (token,tenant))
        interview = cursor.fetchone()
        if not interview:
            raise ValueError('Completed interview unavailable')
        cursor.execute("""SELECT question_number,question,answer FROM interview_answers
            WHERE token=? AND question_number BETWEEN 1 AND 10 ORDER BY question_number,id""",(token,))
        answers = {}
        for row in cursor.fetchall():
            answers.setdefault(row['question_number'],row)
    if len(answers) != 10:
        raise ValueError('Ten saved answers are required')
    if insufficient_answer_evidence(answers):
        return INSUFFICIENT_EVIDENCE_REPORT
    transcript = '\n\n'.join(f"Soru {n}: {row['question']}\nCevap: {row['answer']}" for n,row in answers.items())
    result = generate(cv_text=relevant_text(interview['cv_text'] or ''), position=interview['position'] or '',
                      interview_answers=relevant_text(transcript)).strip()
    if not result:
        raise ValueError('Empty assessment')
    return result


def process_one(generate):
    job = claim()
    if not job:
        return False
    token, tenant, claim_id = job
    report = None
    try:
        report = evaluate(token,tenant,generate)
    except Exception as error:
        logger.warning('Interview assessment failed (%s)',type(error).__name__)
    stamp = datetime.now(timezone.utc).isoformat()
    with write(tenant) as cursor:
        cursor.execute("""UPDATE ik_interview_assessments SET status=?,updated_at=?,claim_id=NULL,lease_until=NULL
            WHERE token=? AND company_id=? AND claim_id=?""",
            ('completed' if report else 'failed',stamp,token,tenant,claim_id))
        if cursor.rowcount and report:
            cursor.execute("""INSERT INTO interview_reports(token,job_fit_analysis,created_at) VALUES (?,?,?)
                ON CONFLICT(token) DO NOTHING""",(token,report,stamp))
    return True


async def worker(generate, stop):
    while not stop.is_set():
        try:
            if await asyncio.to_thread(process_one,generate):
                continue
        except Exception as error:
            logger.warning('Interview assessment worker unavailable (%s)',type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(),timeout=0.5)
        except TimeoutError:
            pass
