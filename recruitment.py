from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field
from typing import Literal

import database


EVALUATIONS = ('new', 'review', 'positive', 'negative')
MANAGERS = ('supervisor', 'hr_manager')


class Position(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    department: str = Field(default='', max_length=120)
    department_id: str | None = Field(default=None,max_length=64)
    location: str = Field(default='', max_length=120)
    description: str = Field(default='', max_length=4000)
    qualifications: str = Field(default='', max_length=4000)
    open: bool = True
    code: str = Field(default='', max_length=36)
    state: Literal['draft','open','paused','closed'] | None = None
    work_mode: str = Field(default='', max_length=40)
    job_family: str = Field(default='', max_length=100)
    worker_group: str = Field(default='', max_length=100)
    matching_criteria: str = Field(default='', max_length=6000)
    employment_type: str = Field(default='', max_length=40)
    salary_range: str = Field(default='', max_length=120)
    experience: str = Field(default='', max_length=300)
    seniority: str = Field(default='', max_length=100)
    education: str = Field(default='', max_length=300)
    certifications: str = Field(default='', max_length=300)
    hiring_contact: str = Field(default='', max_length=120)
    openings: int = Field(default=1, ge=1, le=10000)
    opens_on: str = Field(default='', max_length=10)
    closes_on: str = Field(default='', max_length=10)


class AtsCandidate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    email: str = Field(default='', max_length=254)
    phone: str = Field(default='', max_length=50)
    position_id: str = Field(min_length=1, max_length=64)


class Evaluation(BaseModel):
    evaluation_status: Literal['new', 'review', 'positive', 'negative']


class Assignment(BaseModel):
    assigned_user_id: int | None = Field(default=None, gt=0)


@contextmanager
def write(company_id):
    with closing(database.get_connection()) as connection:
        cursor = connection.cursor()
        try:
            if database.DATABASE_URL:
                if company_id is not None:
                    cursor.execute('SELECT pg_advisory_xact_lock(?)', (company_id,))
            else:
                cursor.execute('BEGIN IMMEDIATE')
            yield cursor
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def scope(actor):
    query = 'company_id IS NOT DISTINCT FROM ?'
    if actor.role == 'hr_staff':
        return query + """ AND ((application_id IS NULL AND (created_by=? OR assigned_user_id=?))
            OR EXISTS(SELECT 1 FROM ats_applications a WHERE a.id=interview_links.application_id
                AND a.company_id=interview_links.company_id AND (a.created_by=? OR a.assigned_user_id=?)))""", (
            actor.company_id, *([actor.user_id]*4))
    return query, (actor.company_id,)


def record(cursor, actor, event, target):
    cursor.execute('INSERT INTO recruitment_audit VALUES (?,?,?,?,?,?)',
                   (uuid4().hex, actor.company_id, actor.user_id, event, target, datetime.now(timezone.utc).isoformat()))


def position_name(cursor, tenant, position_id):
    cursor.execute('SELECT name FROM company_positions WHERE id=? AND company_id=? AND open=1', (position_id, tenant))
    selected = cursor.fetchone()
    if selected is None:
        raise HTTPException(404, 'Açık pozisyon bulunamadı.')
    return selected['name']


def positions(actor):
    with closing(database.get_connection()) as connection:
        cursor = connection.cursor()
        candidate_filter = ''
        application_filter = ''
        position_filter = ''
        parameters = (actor.company_id,)
        if actor.role == 'hr_staff':
            candidate_filter = ' AND (i.created_by=? OR i.assigned_user_id=?)'
            application_filter = ' AND (a.created_by=? OR a.assigned_user_id=?)'
            position_filter = ''' AND EXISTS(SELECT 1 FROM ats_position_members m WHERE
                m.company_id=p.company_id AND m.position_id=p.id AND m.user_id=?)'''
            parameters = (*([actor.user_id]*4), actor.company_id, actor.user_id)
        cursor.execute(f"""SELECT p.*,f.code,f.state,f.fields,
            (SELECT COUNT(*) FROM interview_links i WHERE i.company_id=p.company_id AND i.position_id=p.id
                AND i.application_id IS NULL {candidate_filter}) +
            (SELECT COUNT(*) FROM ats_applications a WHERE a.company_id=p.company_id AND a.position_id=p.id
                {application_filter}) AS candidate_count
            FROM company_positions p LEFT JOIN ats_position_profiles f ON f.position_id=p.id AND f.company_id=p.company_id
            WHERE p.company_id=? {position_filter} ORDER BY p.open DESC,p.name,p.id""", parameters)
        result = []
        for row in cursor.fetchall():
            item = dict(row)
            item.update(json.loads(item.pop('fields') or '{}'))
            item['state'] = item['state'] or ('open' if row['open'] else 'closed')
            result.append(item)
        return result


def save_position(actor, fields, position_id=None):
    values = tuple(value.strip() for value in (fields.name, fields.department, fields.location,
                                               fields.description, fields.qualifications))
    if not values[0]:
        raise HTTPException(400, 'Pozisyon adı boş olamaz.')
    state = fields.state or ('open' if fields.open else 'closed')
    try:
        from datetime import date
        milestones = [date.fromisoformat(value) if value else None for value in (fields.opens_on,fields.closes_on)]
        if all(milestones) and milestones[0] > milestones[1]:
            raise ValueError
    except ValueError:
        raise HTTPException(400,'Geçerli pozisyon tarihleri girin.') from None
    if fields.matching_criteria.strip():
        from ats_matching import parse_criteria
        parse_criteria(fields.matching_criteria)
    metadata = fields.model_dump(include={'work_mode','job_family','worker_group','matching_criteria','employment_type','salary_range','experience','seniority','education','certifications',
                                          'hiring_contact','openings','opens_on','closes_on','department_id'})
    with write(actor.company_id) as cursor:
        if fields.department_id:
            cursor.execute('SELECT name FROM ats_departments WHERE company_id=? AND id=?',
                           (actor.company_id,fields.department_id))
            department = cursor.fetchone()
            if not department:
                raise HTTPException(404,'Firma departmanı bulunamadı.')
            values = (values[0], department['name'], *values[2:])
        else:
            raise HTTPException(400,'Firma departmanı seçin.')
        if position_id:
            cursor.execute("""UPDATE company_positions SET name=?,department=?,location=?,description=?,qualifications=?,open=?
                WHERE id=? AND company_id=?""", (*values, int(state=='open'), position_id, actor.company_id))
            if not cursor.rowcount:
                raise HTTPException(404, 'Pozisyon bulunamadı.')
            event = 'position_changed'
        else:
            position_id = uuid4().hex
            cursor.execute('INSERT INTO company_positions VALUES (?,?,?,?,?,?,?,?,?)',
                           (position_id, actor.company_id, *values, int(state=='open'), datetime.now(timezone.utc).isoformat()))
            event = 'position_created'
        cursor.execute('SELECT code FROM ats_position_profiles WHERE position_id=?',(position_id,))
        old = cursor.fetchone()
        code = fields.code.strip().upper() or (old['code'] if old else 'P-' + position_id[:10].upper())
        cursor.execute('SELECT 1 FROM ats_position_profiles WHERE company_id=? AND code=? AND position_id<>?',
                       (actor.company_id,code,position_id))
        if cursor.fetchone():
            raise HTTPException(409,'Pozisyon kodu kullanımda.')
        cursor.execute("""INSERT INTO ats_position_profiles VALUES (?,?,?,?,?)
            ON CONFLICT(position_id) DO UPDATE SET code=excluded.code,state=excluded.state,fields=excluded.fields""",
            (position_id,actor.company_id,code,state,json.dumps(metadata)))
        record(cursor, actor, event, position_id)
    return {'id': position_id}


def departments(actor):
    with closing(database.get_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute('SELECT id,name FROM ats_departments WHERE company_id=? ORDER BY name',
                       (actor.company_id,))
        return [dict(row) for row in cursor.fetchall()]


def add_department(actor, name):
    name = name.strip()
    if not name:
        raise HTTPException(400,'Departman adı zorunludur.')
    with write(actor.company_id) as cursor:
        cursor.execute('SELECT 1 FROM ats_departments WHERE company_id=? AND name_key=?',
                       (actor.company_id,name.casefold()))
        if cursor.fetchone():
            raise HTTPException(409,'Bu departman zaten kayıtlı.')
        identifier = uuid4().hex
        cursor.execute('INSERT INTO ats_departments VALUES (?,?,?,?)',
                       (identifier,actor.company_id,name,name.casefold()))
        record(cursor,actor,'department_created',identifier)
    return {'id':identifier,'name':name}


def rename_department(actor, identifier, value):
    name = value.strip()
    if not name:
        raise HTTPException(400,'Departman adı zorunludur.')
    with write(actor.company_id) as cursor:
        cursor.execute('SELECT id FROM ats_departments WHERE company_id=? AND name_key=? AND id<>?',
                       (actor.company_id,name.casefold(),identifier))
        if cursor.fetchone():
            raise HTTPException(409,'Bu departman adı zaten kayıtlı.')
        cursor.execute('UPDATE ats_departments SET name=?,name_key=? WHERE id=? AND company_id=?',
                       (name,name.casefold(),identifier,actor.company_id))
        if not cursor.rowcount:
            raise HTTPException(404,'Firma departmanı bulunamadı.')
        cursor.execute('SELECT position_id,fields FROM ats_position_profiles WHERE company_id=?',(actor.company_id,))
        for position in cursor.fetchall():
            if json.loads(position['fields']).get('department_id') == identifier:
                cursor.execute('UPDATE company_positions SET department=? WHERE id=? AND company_id=?',
                               (name,position['position_id'],actor.company_id))
        record(cursor,actor,'department_renamed',identifier)
    return {'id':identifier,'name':name}


def close_position(actor, identifier):
    with write(actor.company_id) as cursor:
        cursor.execute('UPDATE company_positions SET open=0 WHERE id=? AND company_id=?',
                       (identifier,actor.company_id))
        if not cursor.rowcount:
            raise HTTPException(404,'Pozisyon bulunamadı.')
        cursor.execute("UPDATE ats_position_profiles SET state='closed' WHERE position_id=? AND company_id=?",
                       (identifier,actor.company_id))
        record(cursor,actor,'position_closed',identifier)
    return {'success':True}


def monthly_usage(cursor, actor):
    if len(actor.month_bounds) != 2:
        raise HTTPException(503, 'Mülakat kota dönemi doğrulanamadı.')
    cursor.execute("""SELECT COUNT(*) AS usage FROM interview_links
        WHERE company_id=? AND created_at>=? AND created_at<? AND expires_at IS NOT NULL""",
        (actor.company_id, *actor.month_bounds))
    return cursor.fetchone()['usage']


def enforce_interview_quota(cursor, actor):
    if actor.interview_quota is not None and monthly_usage(cursor, actor) >= actor.interview_quota:
        raise HTTPException(409, 'Aylık mülakat kotası doldu.')


def create_ats_candidate(actor, details):
    token = uuid4().hex + uuid4().hex
    name = details.name.strip()
    if not name:
        raise HTTPException(400, 'Aday adı boş olamaz.')
    with write(actor.company_id) as cursor:
        position = position_name(cursor, actor.company_id, details.position_id)
        staff_position(cursor, actor, details.position_id)
        cursor.execute("""INSERT INTO interview_links
            (token,name,email,phone,company,position,status,created_at,company_id,position_id,created_by)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (token, name, details.email.strip(), details.phone.strip(), actor.company_name,
                position, 'new', datetime.now(timezone.utc).isoformat(), actor.company_id, details.position_id, actor.user_id))
        record(cursor, actor, 'ats_candidate_created', token)
    return {'token': token}


def staff_position(cursor, actor, position_id):
    if actor.role == 'hr_staff':
        cursor.execute('SELECT 1 FROM ats_position_members WHERE company_id=? AND position_id=? AND user_id=?',
                       (actor.company_id,position_id,actor.user_id))
        if not cursor.fetchone():
            raise HTTPException(403,'Bu pozisyonda işlem yetkiniz yok.')


def change_candidate(actor, token, fields):
    field, value, event = ('evaluation_status', fields.evaluation_status, 'candidate_evaluation_changed') if isinstance(
        fields, Evaluation) else ('assigned_user_id', fields.assigned_user_id, 'candidate_assignment_changed')
    with write(actor.company_id) as cursor:
        cursor.execute(f'UPDATE interview_links SET {field}=? WHERE token=? AND company_id=?',
                       (value, token, actor.company_id))
        if not cursor.rowcount:
            raise HTTPException(404, 'Aday bulunamadı.')
        record(cursor, actor, event, token)
    return {'success': True}
