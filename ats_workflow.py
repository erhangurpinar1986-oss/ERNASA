import json
from collections import Counter
from contextlib import closing, nullcontext
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import BaseModel, Field

from ats import STAGES, application, author_clause, now, require_staff_position, uid
from database import get_connection
from recruitment import MANAGERS, record, write


class Move(BaseModel):
    stage: Literal['new','screening','shortlist','interview','evaluation','offer','hired']
    revision: int = Field(ge=0)
    closure: Literal['negative','withdrawn','position_closed'] | None = None


class Schedule(BaseModel):
    interviewer_id: int = Field(gt=0)
    starts_at: str = Field(max_length=40)
    ends_at: str = Field(max_length=40)
    timezone: str = Field(default='Europe/Istanbul', max_length=80)
    kind: Literal['video','onsite','phone']
    status: Literal['planned','completed','cancelled'] = 'planned'
    notes: str = Field(default='', max_length=3000)
    revision: int = Field(default=0,ge=0)


class Criteria(BaseModel):
    criteria: list[str] = Field(min_length=1,max_length=15)


class Review(BaseModel):
    scores: dict[str,int] = Field(max_length=15)
    note: str = Field(default='',max_length=4000)
    strengths: str = Field(default='',max_length=3000)
    improvements: str = Field(default='',max_length=3000)
    completed: bool = False


class ReviewDecision(BaseModel):
    state: Literal['approved','rejected']


class OfferCreate(BaseModel):
    amount_minor: int = Field(gt=0, le=1_000_000_000_00)
    currency: Literal['TRY','EUR','USD']
    starts: date
    terms: str = Field(min_length=1,max_length=8000)
    pay_basis: Literal['gross','net'] | None = None
    benefits: str = Field(default='',max_length=4000)
    valid_until: date | None = None


class OfferChange(BaseModel):
    status: Literal['approved','ready','sent','accepted','rejected','withdrawn']
    revision: int = Field(ge=0)


def position(cursor, actor, position_id):
    cursor.execute('SELECT id FROM company_positions WHERE company_id=? AND id=?',(actor.company_id,position_id))
    if not cursor.fetchone():
        raise HTTPException(404,'Pozisyon bulunamadı.')


def move(actor, app_id, fields):
    with write(actor.company_id) as cursor:
        row = application(cursor,actor,app_id)
        if actor.role not in MANAGERS:
            require_staff_position(cursor,actor,row['position_id'])
            if ('ats_stage' not in actor.permissions or fields.closure or row['closure'] or
                    fields.stage in ('offer','hired') or row['stage'] in ('offer','hired')):
                raise HTTPException(403,'Bu aşama değişikliği firma yöneticisi yetkisi gerektirir.')
        if fields.revision != row['revision']:
            raise HTTPException(409,'Başvuru değişti. Panoyu yenileyin.')
        if fields.stage == 'hired' and not fields.closure:
            cursor.execute("SELECT 1 FROM ats_offers WHERE company_id=? AND application_id=? AND status='accepted'",
                           (actor.company_id,app_id))
            if not cursor.fetchone():
                raise HTTPException(409,'İşe alım için kabul edilmiş teklif gerekir.')
        if (row['stage'],row['closure']) == (fields.stage,fields.closure):
            return {'revision':row['revision']}
        cursor.execute("""UPDATE ats_applications SET stage=?,closure=?,changed_at=?,revision=revision+1
            WHERE company_id=? AND id=? AND revision=?""",
            (fields.stage,fields.closure,now(),actor.company_id,app_id,fields.revision))
        cursor.execute('INSERT INTO ats_stage_events VALUES (?,?,?,?,?,?,?,?)',
                       (uid(),actor.company_id,app_id,actor.user_id,row['stage'],fields.stage,fields.closure,now()))
        record(cursor,actor,'application_stage_changed',app_id)
    return {'revision':fields.revision+1}


def timezone_for(name):
    try:
        return ZoneInfo(name)
    except (ValueError,ZoneInfoNotFoundError):
        if name == 'UTC':
            return timezone.utc
        if name == 'Europe/Istanbul':
            with Path(__file__).with_name('Europe-Istanbul.tzif').open('rb') as zone:
                return ZoneInfo.from_file(zone,key=name)
        raise HTTPException(400,'Desteklenen bir IANA saat dilimi girin.') from None


def time_bounds(fields):
    zone = timezone_for(fields.timezone)
    try:
        times = [datetime.fromisoformat(value) for value in (fields.starts_at,fields.ends_at)]
    except ValueError:
        raise HTTPException(400,'Geçerli başlangıç ve bitiş zamanı girin.') from None
    utc = []
    for value in times:
        local = value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)
        candidate = local.astimezone(timezone.utc)
        if value.tzinfo is None and candidate.astimezone(zone).replace(tzinfo=None) != value:
            raise HTTPException(400,'Yerel saat geçişi nedeniyle bu zaman geçersiz.')
        utc.append(candidate)
    if utc[0]>=utc[1]:
        raise HTTPException(400,'Bitiş zamanı başlangıçtan sonra olmalıdır.')
    return tuple(value.isoformat() for value in utc)


def list_schedules(actor, app_id=None):
    staff, values = author_clause(actor)
    args = [actor.company_id,*values]
    app_filter = ''
    if app_id:
        app_filter = ' AND s.application_id=?'
        args.append(app_id)
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute(f"""SELECT s.*,c.name,p.name AS position FROM ats_schedules s
             JOIN ats_applications a ON a.id=s.application_id AND a.company_id=s.company_id
             JOIN ats_candidates c ON c.id=a.candidate_id AND c.company_id=a.company_id
             JOIN company_positions p ON p.id=a.position_id AND p.company_id=a.company_id
             WHERE s.company_id=? {staff}{app_filter} ORDER BY s.starts_at DESC LIMIT 200""",args)
        return [dict(row) for row in cursor.fetchall()]


def save_schedule(actor, app_id, fields, schedule_id=None, *, active_cursor=None):
    starts, ends = time_bounds(fields)
    transaction = write(actor.company_id) if active_cursor is None else nullcontext(active_cursor)
    with transaction as cursor:
        row = application(cursor,actor,app_id)
        if actor.role not in MANAGERS:
            require_staff_position(cursor,actor,row['position_id'])
            if 'ats_schedule' not in actor.permissions or fields.interviewer_id != actor.user_id:
                raise HTTPException(403,'Görüşme planlama yetkiniz yok.')
        if row['closure']:
            raise HTTPException(409,'Kapanmış başvuru için görüşme planlanamaz.')
        if schedule_id:
            cursor.execute('SELECT revision,status,interviewer_id FROM ats_schedules WHERE company_id=? AND application_id=? AND id=?',
                           (actor.company_id,app_id,schedule_id))
            old = cursor.fetchone()
            if not old:
                raise HTTPException(404,'Görüşme bulunamadı.')
            if actor.role not in MANAGERS and old['interviewer_id']!=actor.user_id:
                raise HTTPException(403,'Yalnız kendi görüşmenizi değiştirebilirsiniz.')
            if old['revision']!=fields.revision or old['status']!='planned':
                raise HTTPException(409,'Görüşme güncellendi veya kapatıldı.')
        elif fields.status!='planned':
            raise HTTPException(400,'Yeni görüşme planlı durumda olmalıdır.')
        if fields.status=='planned':
            cursor.execute("""SELECT 1 FROM ats_schedules WHERE company_id=? AND status='planned'
                AND (interviewer_id=? OR application_id=?) AND id<>? AND starts_at<? AND ends_at>?""",
                (actor.company_id,fields.interviewer_id,app_id,schedule_id or '',ends,starts))
            if cursor.fetchone():
                raise HTTPException(409,'Bu zaman dilimi mevcut görüşmeyle çakışıyor.')
        values = (fields.interviewer_id,starts,ends,fields.timezone,fields.kind,fields.status,fields.notes.strip())
        if schedule_id:
            cursor.execute("""UPDATE ats_schedules SET interviewer_id=?,starts_at=?,ends_at=?,timezone=?,kind=?,status=?,notes=?,
                revision=revision+1 WHERE company_id=? AND application_id=? AND id=?""",(*values,actor.company_id,app_id,schedule_id))
        else:
            schedule_id = uid()
            cursor.execute("""INSERT INTO ats_schedules (id,company_id,application_id,interviewer_id,starts_at,ends_at,
                timezone,kind,status,notes) VALUES (?,?,?,?,?,?,?,?,?,?)""",(schedule_id,actor.company_id,app_id,*values))
        record(cursor,actor,'schedule_'+fields.status,schedule_id)
    return {'id':schedule_id}


def position_criteria(cursor, tenant, position_id):
    cursor.execute('SELECT criteria FROM ats_criteria WHERE company_id=? AND position_id=?',(tenant,position_id))
    saved = cursor.fetchone()
    return json.loads(saved['criteria']) if saved else []


def save_criteria(actor, position_id, fields):
    criteria = [item.strip() for item in fields.criteria]
    if any(not item or len(item)>120 for item in criteria) or len(set(criteria))!=len(criteria):
        raise HTTPException(400,'Kriterler benzersiz ve 1–120 karakter olmalıdır.')
    with write(actor.company_id) as cursor:
        position(cursor,actor,position_id)
        cursor.execute("""INSERT INTO ats_criteria VALUES (?,?,?)
            ON CONFLICT(position_id) DO UPDATE SET criteria=excluded.criteria""",
            (position_id,actor.company_id,json.dumps(criteria)))
        record(cursor,actor,'evaluation_criteria_changed',position_id)
    return {'criteria':criteria}


def list_reviews(actor, app_id):
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        row = application(cursor,actor,app_id)
        where = '' if actor.role in MANAGERS else ' AND e.reviewer_id=?'
        params = () if actor.role in MANAGERS else (actor.user_id,)
        cursor.execute("""SELECT e.*,COALESCE(d.strengths,'') AS strengths,COALESCE(d.improvements,'') AS improvements,
            COALESCE(d.state,'pending') AS state,d.decided_at FROM ats_evaluations e
            LEFT JOIN ats_review_details d ON d.evaluation_id=e.id AND d.company_id=e.company_id
            WHERE e.company_id=? AND e.application_id=?"""+where,
                       (actor.company_id,app_id,*params))
        reviews = [{**dict(rec),'scores':json.loads(rec['scores'])} for rec in cursor.fetchall()]
        return {'criteria':position_criteria(cursor,actor.company_id,row['position_id']),'evaluations':reviews}


def review(actor, app_id, fields):
    with write(actor.company_id) as cursor:
        row = application(cursor,actor,app_id)
        if actor.role not in MANAGERS:
            cursor.execute("""SELECT 1 FROM ats_schedules WHERE company_id=? AND application_id=?
                AND interviewer_id=? AND status='completed'""",(actor.company_id,app_id,actor.user_id))
            if not cursor.fetchone():
                raise HTTPException(403,'Bu başvuru için değerlendirici yetkiniz yok.')
        criteria = position_criteria(cursor,actor.company_id,row['position_id'])
        if not criteria or not set(fields.scores)<=set(criteria) or any(
                value<1 or value>5 for value in fields.scores.values()):
            raise HTTPException(400,'Puanlar yalnızca pozisyon kriterleri için 1–5 arasında olmalıdır.')
        if fields.completed and set(fields.scores)!=set(criteria):
            raise HTTPException(400,'Tamamlamak için bütün kriterleri puanlayın.')
        cursor.execute('SELECT completed FROM ats_evaluations WHERE company_id=? AND application_id=? AND reviewer_id=?',
                       (actor.company_id,app_id,actor.user_id))
        old = cursor.fetchone()
        if old and old['completed']:
            raise HTTPException(409,'Tamamlanan değerlendirme değiştirilemez.')
        cursor.execute("""INSERT INTO ats_evaluations VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(company_id,application_id,reviewer_id) DO UPDATE SET scores=excluded.scores,
            note=excluded.note,completed=excluded.completed""",
            (uid(),actor.company_id,app_id,actor.user_id,json.dumps(fields.scores),fields.note.strip(),int(fields.completed),now()))
        cursor.execute('SELECT id FROM ats_evaluations WHERE company_id=? AND application_id=? AND reviewer_id=?',
                       (actor.company_id,app_id,actor.user_id))
        evaluation_id = cursor.fetchone()['id']
        cursor.execute("""INSERT INTO ats_review_details(evaluation_id,company_id,strengths,improvements)
            VALUES (?,?,?,?) ON CONFLICT(evaluation_id) DO UPDATE
            SET strengths=excluded.strengths,improvements=excluded.improvements""",
            (evaluation_id,actor.company_id,fields.strengths.strip(),fields.improvements.strip()))
        record(cursor,actor,'evaluation_completed' if fields.completed else 'evaluation_drafted',app_id)
    return {'success':True}


def decide_review(actor, app_id, evaluation_id, fields):
    with write(actor.company_id) as cursor:
        application(cursor,actor,app_id)
        cursor.execute("""SELECT e.completed,COALESCE(d.state,'pending') AS state FROM ats_evaluations e
            LEFT JOIN ats_review_details d ON e.id=d.evaluation_id AND e.company_id=d.company_id
            WHERE e.company_id=? AND e.application_id=? AND e.id=?""",(actor.company_id,app_id,evaluation_id))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404,'Değerlendirme bulunamadı.')
        if not row['completed'] or row['state']!='pending':
            raise HTTPException(409,'Yalnızca tamamlanmış ve bekleyen değerlendirmeler karara bağlanabilir.')
        cursor.execute("""INSERT INTO ats_review_details VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(evaluation_id) DO UPDATE SET state=excluded.state,approved_by=excluded.approved_by,decided_at=excluded.decided_at""",
            (evaluation_id,actor.company_id,'','',fields.state,actor.user_id,now()))
        record(cursor,actor,'evaluation_'+fields.state,evaluation_id)
    return {'state':fields.state}


OFFER_NEXT = {'draft':('approved','withdrawn'),'approved':('ready','withdrawn'),
              'ready':('sent','withdrawn'),'sent':('accepted','rejected','withdrawn')}


def offers(actor, app_id):
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        application(cursor,actor,app_id)
        cursor.execute('SELECT * FROM ats_offers WHERE company_id=? AND application_id=? ORDER BY created_at DESC',
                       (actor.company_id,app_id))
        return [dict(row) for row in cursor.fetchall()]


def create_offer(actor, app_id, fields):
    with write(actor.company_id) as cursor:
        row = application(cursor,actor,app_id)
        if row['closure'] or not fields.terms.strip():
            raise HTTPException(400,'Açık başvuru ve teklif koşulları gerekir.')
        offer_id = uid()
        cursor.execute("""INSERT INTO ats_offers(id,company_id,application_id,amount_minor,currency,starts,terms,status,created_at)
            VALUES (?,?,?,?,?,?,?,'draft',?)""",
            (offer_id,actor.company_id,app_id,fields.amount_minor,fields.currency,fields.starts.isoformat(),fields.terms.strip(),now()))
        cursor.execute('INSERT INTO ats_offer_details VALUES (?,?,?,?,?)',
                       (offer_id,actor.company_id,fields.pay_basis,fields.benefits.strip(),
                        fields.valid_until.isoformat() if fields.valid_until else None))
        record(cursor,actor,'offer_drafted',offer_id)
    return {'id':offer_id}


def offer_document(actor, app_id, offer_id):
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        candidate = application(cursor,actor,app_id)
        cursor.execute("""SELECT o.*,d.pay_basis,d.benefits,d.valid_until,p.description
            FROM ats_offers o JOIN ats_applications a ON a.id=o.application_id AND a.company_id=o.company_id
            JOIN company_positions p ON p.id=a.position_id AND p.company_id=a.company_id
            LEFT JOIN ats_offer_details d ON d.offer_id=o.id AND d.company_id=o.company_id
            WHERE o.company_id=? AND o.application_id=? AND o.id=?""", (actor.company_id,app_id,offer_id))
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(404,'Teklif bulunamadı.')
        return candidate,dict(row)


def change_offer(actor, app_id, offer_id, fields):
    with write(actor.company_id) as cursor:
        application(cursor,actor,app_id)
        cursor.execute('SELECT status,revision FROM ats_offers WHERE company_id=? AND application_id=? AND id=?',
                       (actor.company_id,app_id,offer_id))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404,'Teklif bulunamadı.')
        if fields.revision!=row['revision'] or fields.status not in OFFER_NEXT.get(row['status'],()):
            raise HTTPException(409,'Teklif güncellendi veya durum geçişi geçersiz.')
        cursor.execute('UPDATE ats_offers SET status=?,revision=revision+1 WHERE company_id=? AND id=?',
                       (fields.status,actor.company_id,offer_id))
        record(cursor,actor,'offer_'+fields.status,offer_id)
    return {'revision':fields.revision+1}


def metrics(actor):
    staff, params = author_clause(actor)
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        if actor.role in MANAGERS:
            cursor.execute('SELECT COUNT(*) AS n FROM company_positions WHERE company_id=? AND open=1',
                           (actor.company_id,))
        else:
            cursor.execute("""SELECT COUNT(*) AS n FROM company_positions p JOIN ats_position_members m ON
                m.position_id=p.id AND m.company_id=p.company_id WHERE p.company_id=? AND p.open=1 AND m.user_id=?""",
                (actor.company_id,actor.user_id))
        open_positions = cursor.fetchone()['n']
        cursor.execute(f"""SELECT a.stage,a.closure,a.created_at,a.candidate_id,a.position_id,p.name,
            (SELECT MIN(e.occurred_at) FROM ats_stage_events e WHERE e.application_id=a.id
                AND e.company_id=a.company_id AND e.next='hired' AND e.closure IS NULL) AS hired_at
            FROM ats_applications a JOIN company_positions p ON p.id=a.position_id AND p.company_id=a.company_id
            WHERE a.company_id=? {staff}""", (actor.company_id,*params))
        rows = cursor.fetchall()
        cursor.execute(f"""SELECT COUNT(*) AS n
            FROM ats_schedules s JOIN ats_applications a ON a.id=s.application_id AND a.company_id=s.company_id
            WHERE a.company_id=? AND s.status='completed' {staff}""", (actor.company_id,*params))
        completed_schedules = cursor.fetchone()['n']
    zone = timezone_for('Europe/Istanbul')
    months, positions = Counter(), Counter()
    stages = Counter(row['stage'] for row in rows if not row['closure'])
    durations = []
    for row in rows:
        created = datetime.fromisoformat(row['created_at'])
        months[created.astimezone(zone).strftime('%Y-%m')] += 1
        positions[(row['position_id'],row['name'])] += 1
        if not row['closure'] and row['stage']=='hired' and row['hired_at']:
            durations.append((datetime.fromisoformat(row['hired_at'])-created).total_seconds()/86400)
    return {'open_positions':open_positions,'candidates':len({row['candidate_id'] for row in rows}),
            'applications':len(rows),'completed_schedules':completed_schedules,
            'hiring_days':round(sum(durations)/len(durations),1) if durations else None,
            'stages':{stage:stages[stage] for stage in STAGES},'months':dict(sorted(months.items())[-12:]),
            'positions':[{'id':key[0],'name':key[1],'applications':count} for key,count in positions.most_common()]}


class Retention(BaseModel):
    retention_days: int = Field(ge=1,le=3650)


class PrivacyRequest(BaseModel):
    kind: Literal['erase','anonymize','export']


class PrivacyDecision(BaseModel):
    status: Literal['approved','rejected']


def privacy(actor, retention_days=None):
    if retention_days is not None:
        with write(actor.company_id) as cursor:
            cursor.execute("""INSERT INTO ats_privacy_policy VALUES (?,?,?)
                ON CONFLICT(company_id) DO UPDATE SET retention_days=excluded.retention_days,updated_at=excluded.updated_at""",
                (actor.company_id,retention_days,now()))
            record(cursor,actor,'privacy_retention_configured',str(actor.company_id))
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute('SELECT retention_days,updated_at FROM ats_privacy_policy WHERE company_id=?',(actor.company_id,))
        settings = cursor.fetchone()
        cursor.execute("""SELECT id,application_id,kind,status,created_at FROM ats_privacy_requests
            WHERE company_id=? ORDER BY created_at DESC LIMIT 100""",(actor.company_id,))
        return {'policy':dict(settings) if settings else None,'requests':[dict(row) for row in cursor.fetchall()]}


def request_privacy(actor, app_id, fields):
    request_id = uid()
    with write(actor.company_id) as cursor:
        application(cursor,actor,app_id)
        cursor.execute("""SELECT id FROM ats_privacy_requests
            WHERE company_id=? AND application_id=? AND kind=? AND status='requested'""",
            (actor.company_id,app_id,fields.kind))
        old = cursor.fetchone()
        if old:
            return {'id':old['id'],'status':'requested'}
        cursor.execute('INSERT INTO ats_privacy_requests VALUES (?,?,?,?,?,?,?)',
                       (request_id,actor.company_id,app_id,actor.user_id,fields.kind,'requested',now()))
        record(cursor,actor,'privacy_request_'+fields.kind,app_id)
    return {'id':request_id,'status':'requested'}


def decide_privacy(actor, request_id, fields):
    with write(actor.company_id) as cursor:
        cursor.execute('SELECT status FROM ats_privacy_requests WHERE company_id=? AND id=?',
                       (actor.company_id,request_id))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404,'Gizlilik talebi bulunamadı.')
        if row['status'] != 'requested':
            raise HTTPException(409,'Gizlilik talebi daha önce incelendi.')
        cursor.execute('UPDATE ats_privacy_requests SET status=? WHERE company_id=? AND id=?',
                       (fields.status,actor.company_id,request_id))
        record(cursor,actor,'privacy_request_'+fields.status,request_id)
    return {'status':fields.status}


def export_application(actor, app_id):
    from ats import details
    payload = details(actor,app_id)
    with write(actor.company_id) as cursor:
        application(cursor,actor,app_id)
        cursor.execute("""SELECT notice,notice_version,acknowledged_at FROM ats_notices
            WHERE company_id=? AND application_id=?""",(actor.company_id,app_id))
        notice = cursor.fetchone()
        payload['notice'] = dict(notice) if notice else None
        record(cursor,actor,'application_metadata_exported',app_id)
    payload['evaluations'] = list_reviews(actor,app_id)['evaluations']
    return payload
