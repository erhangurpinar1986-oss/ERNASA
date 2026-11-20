from contextlib import closing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json

from fastapi import HTTPException

from ats_matching import relevant_text
from database import get_connection
from interview_assessments import enqueue
from recruitment import write
from control.interview_templates import snapshot_question_count


def load_interview(cursor, token, company_id):
    cursor.execute("""SELECT i.*,p.question_count AS partner_question_count, template.questions AS template_questions,
        intro.question AS introduction_question, intro.answer AS introduction_answer
        FROM interview_links i LEFT JOIN ik_partner_interviews p ON p.token=i.token AND p.company_id=i.company_id
        LEFT JOIN ik_partner_introductions intro ON intro.token=p.token AND intro.company_id=p.company_id
        LEFT JOIN ik_interview_template_snapshots template ON template.token=i.token AND template.company_id=i.company_id
        WHERE i.token=? AND i.company_id=? AND i.status IS DISTINCT FROM 'new'""", (token, company_id))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(404, 'Mülakat bağlantısı bulunamadı.')
    interview = dict(row)
    snapshot = {'questions':interview['template_questions']} if interview['template_questions'] else None
    interview['question_count'] = interview['partner_question_count'] or snapshot_question_count(snapshot)
    return interview


def introduction_note(cursor, token, tenant):
    if tenant is None:
        return None
    cursor.execute('SELECT answer FROM ik_partner_introductions WHERE token=? AND company_id=?', (token, tenant))
    row = cursor.fetchone()
    return row['answer'] if row else None


def introduction_pending(interview):
    return interview['introduction_question'] is not None and interview['introduction_answer'] is None


def answers(cursor, token, total=10):
    cursor.execute("""SELECT question_number,question,answer FROM interview_answers WHERE token=?
        AND question_number BETWEEN 1 AND ? ORDER BY question_number,id""", (token,total))
    result = {}
    for row in cursor.fetchall():
        result.setdefault(row['question_number'], row)
    if total > 10:
        cursor.execute("""SELECT question_number,question,answer FROM ik_partner_extra_questions
            WHERE token=? AND question_number<=? AND answer IS NOT NULL ORDER BY question_number""",(token,total))
        result.update((row['question_number'],row) for row in cursor.fetchall())
    return result


def question_sql(number):
    table = 'interview_questions' if number <= 10 else 'ik_partner_extra_questions'
    return f'SELECT question FROM {table} WHERE token=? AND question_number=?'


def completed(total=10):
    return {'success':True, 'completed':True, 'message':'Mülakat tamamlandı.',
            'question_number':total, 'question_count':total, 'question':None}


def interview_job_context(cursor, interview):
    if not interview['position_id']:
        return ''
    cursor.execute("""SELECT p.description,p.qualifications,f.fields FROM company_positions p
        LEFT JOIN ats_position_profiles f ON f.position_id=p.id AND f.company_id=p.company_id
        WHERE p.company_id=? AND p.id=?""",(interview['company_id'],interview['position_id']))
    row = cursor.fetchone()
    if not row:
        return ''
    fields = json.loads(row['fields'] or '{}')
    return relevant_text('\n'.join([row['description'],row['qualifications'],
        fields.get('seniority',''),fields.get('experience','')]))[:3200]


def current_question(interview, recorded, intro, generate):
    total = interview['question_count']
    if introduction_pending(interview):
        return {'success':True, 'status':'started', 'phase':'introduction', 'question_number':0,
                'question_count':total, 'question':interview['introduction_question']}
    number = next((n for n in range(1,total+1) if n not in recorded), None)
    if number is None:
        return completed(total)
    token, tenant = interview['token'], interview['company_id']
    lookup = question_sql(number)
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute(lookup, (token, number))
        row = cursor.fetchone()
        job_context = interview_job_context(cursor, interview) if row is None and number != 1 else ''
    if row is None:
        if total > 10 or interview["template_questions"]:
            raise HTTPException(503,'Kilitli mülakat sorusu bulunamadı.')
        history = '\n'.join(f"Soru {n}: {row['question']}\nCevap: {row['answer']}" for n,row in recorded.items())
        text = intro() if number == 1 else generate(cv_text=relevant_text(interview['cv_text'] or ''),
                                                    position=interview['position'] or '',
                                                    previous_answers=relevant_text(history), question_number=number,
                                                    job_context=job_context)
        with write(tenant) as cursor:
            cursor.execute("""INSERT INTO interview_questions VALUES (?,?,?)
                ON CONFLICT(token,question_number) DO NOTHING""", (token, number, text))
            cursor.execute(lookup, (token, number))
            row = cursor.fetchone()
    result = {'success':True, 'status':'started', 'question_number':number, 'question_count':total, 'question':row['question']}
    if total > 10:
        result['phase'] = 'locked' if number <= 10 else 'extra'
    return result


def availability(cursor, interview, tenant, now=None):
    """Return the current schedule gate for a tenant-bound interview."""
    if tenant is None or not interview.get("application_id"):
        return None
    cursor.execute("""SELECT s.starts_at,s.status FROM flow_scheduled_interviews f
        LEFT JOIN ats_schedules s ON s.id=f.schedule_id AND s.company_id=f.company_id
            AND s.application_id=?
        WHERE f.token=? AND f.company_id=?""",
        (interview["application_id"],interview["token"],tenant))
    schedule = cursor.fetchone()
    if schedule is None:
        cursor.execute("""SELECT starts_at,status FROM ats_schedules
            WHERE company_id=? AND application_id=? AND status='planned'
            ORDER BY starts_at,id LIMIT 2""", (tenant,interview["application_id"]))
        schedules = cursor.fetchall()
        if len(schedules) > 1:
            return {"state":"unavailable"}
        schedule = schedules[0] if schedules else None
    if schedule is None:
        return None
    if schedule["status"] != "planned" or not schedule["starts_at"]:
        return {"state":"unavailable"}
    try:
        start_at = datetime.fromisoformat(schedule["starts_at"])
        if start_at.tzinfo is None:
            return {"state":"unavailable"}
    except (ValueError, TypeError):
        return {"state":"unavailable"}
    end_at = start_at + timedelta(hours=8)
    now = now or datetime.now(timezone.utc)
    state = "scheduled" if now < start_at else "expired" if now >= end_at else "available"
    return {"state":state,"starts_at":start_at.isoformat(),"expires_at":end_at.isoformat()}


def start(token, tenant, deadline_passed, intro, generate):
    expired = False
    with write(tenant) as cursor:
        interview = load_interview(cursor, token, tenant)
        window = availability(cursor, interview, tenant) if interview["status"] in ("waiting","created","opened") else None
        if window and window["state"] == "unavailable":
            raise HTTPException(409,"Görüşme takvimi için İnsan Kaynakları ile iletişime geçin.")
        if window and window["state"] == "scheduled":
            local = datetime.fromisoformat(window["starts_at"]).astimezone(ZoneInfo("Europe/Istanbul"))
            raise HTTPException(409,f"Görüşme başlangıcı {local:%d.%m.%Y %H:%M} (Türkiye saati).")
        status = interview['status']
        if status == 'completed':
            raise HTTPException(409, 'Bu mülakat zaten tamamlandı.')
        if status == 'expired' or (status in ('waiting','created','opened') and (window['state']=='expired' if window else deadline_passed(interview))):
            cursor.execute("UPDATE interview_links SET status='expired' WHERE token=?", (token,))
            expired = True
        elif status in ('waiting','created','opened'):
            cursor.execute("UPDATE interview_links SET status='started' WHERE token=?", (token,))
        elif status != 'started':
            raise HTTPException(409, 'Mülakat durumu geçersiz.')
        recorded = answers(cursor, token, interview['question_count'])
    if expired:
        raise HTTPException(410, 'Mülakata başlama süresi dolmuş.')
    return current_question(interview, recorded, intro, generate)


def save_introduction(cursor, token, tenant, interview, answer):
    if interview['introduction_question'] is None:
        raise HTTPException(400, 'Bu mülakatta tanışma sorusu yok.')
    saved = interview['introduction_answer']
    if saved is not None:
        if saved != answer:
            raise HTTPException(409, 'Tanışma sorusu daha önce yanıtlandı.')
        return
    if interview['status'] != 'started':
        raise HTTPException(409, 'Mülakat başlamamış veya tamamlanmış.')
    cursor.execute("""UPDATE ik_partner_introductions SET answer=?
        WHERE token=? AND company_id=? AND answer IS NULL""", (answer, token, tenant))
    interview['introduction_answer'] = answer


def submit(token, tenant, payload, intro, generate):
    number = payload.get('question_number')
    answer = payload.get('answer')
    with write(tenant) as cursor:
        interview = load_interview(cursor, token, tenant)
        total = interview['question_count']
        if type(number) is not int or not 0 <= number <= total or not isinstance(answer,str) or not 1 <= len(answer.strip()) <= 12000:
            raise HTTPException(400, 'Soru numarası veya cevap geçersiz.')
        answer = answer.strip()
        recorded = answers(cursor, token, total)
        saved = recorded.get(number)
        if number == 0:
            save_introduction(cursor, token, tenant, interview, answer)
        elif introduction_pending(interview):
            raise HTTPException(409, 'Önce tanışma sorusunu yanıtlayın.')
        elif saved:
            if saved['answer'] != answer:
                raise HTTPException(409, 'Bu soru daha önce yanıtlandı.')
        else:
            if interview['status'] != 'started':
                raise HTTPException(409, 'Mülakat başlamamış veya tamamlanmış.')
            expected = next((n for n in range(1,total+1) if n not in recorded), None)
            cursor.execute(question_sql(number), (token,number))
            question = cursor.fetchone()
            if number != expected or question is None:
                raise HTTPException(409, 'Önce sunucunun gösterdiği sıradaki soruyu yanıtlayın.')
            timestamp = datetime.now(timezone.utc).isoformat()
            if number <= 10:
                cursor.execute("""INSERT INTO interview_answers(token,question_number,question,answer,created_at)
                    VALUES (?,?,?,?,?)""",(token,number,question['question'],answer,timestamp))
            else:
                cursor.execute("""UPDATE ik_partner_extra_questions SET answer=?,answered_at=?
                    WHERE token=? AND question_number=?""",(answer,timestamp,token,number))
            recorded = answers(cursor, token, total)
            if len(recorded) == total:
                cursor.execute("UPDATE interview_links SET status='completed' WHERE token=?", (token,))
                if total >= 10:
                    enqueue(cursor,tenant,token)
    return current_question(interview, recorded, intro, generate)
