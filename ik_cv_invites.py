from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from fastapi import HTTPException
from pydantic import Field

import ats
import ats_cv_import
from database import get_connection
from recruitment import position_name, record, staff_position, write


class Selection(ats_cv_import.Selection):
    new_position: str = Field(default='', max_length=160)


def select_position(cursor, actor, fields):
    position_id = fields.position_id
    if not position_id and 'ats' not in actor.products and fields.new_position.strip():
        name = fields.new_position.strip()
        cursor.execute('SELECT id,name FROM company_positions WHERE company_id=? AND open=1 ORDER BY id',
                       (actor.company_id,))
        matches = [row['id'] for row in cursor.fetchall() if row['name'].casefold() == name.casefold()]
        if len(matches) > 1:
            raise HTTPException(409, 'Bu adda birden fazla açık pozisyon var. Listeden pozisyon seçin.')
        if matches:
            position_id = matches[0]
        else:
            position_id = ats.uid()
            cursor.execute("""INSERT INTO company_positions
                (id,company_id,name,department,location,description,qualifications,open,created_at)
                VALUES (?,?,?,'','','','',1,?)""", (position_id,actor.company_id,name,ats.now()))
            if actor.role == 'hr_staff':
                cursor.execute('INSERT INTO ats_position_members VALUES (?,?,?)',
                               (actor.company_id,position_id,actor.user_id))
            record(cursor,actor,'position_created',position_id)
    if not position_id:
        raise HTTPException(400, 'Açık bir pozisyon seçin.')
    position_name(cursor, actor.company_id, position_id)
    staff_position(cursor, actor, position_id)
    return position_id


def receipt(cursor, actor, request_id):
    cursor.execute("""SELECT r.candidate_number,i.token,i.application_id,i.name,i.email,i.phone,i.company,i.position,
        i.status,i.created_at,i.expires_at FROM ik_cv_invites r JOIN interview_links i
        ON i.token=r.token AND i.company_id=r.company_id
        WHERE r.company_id=? AND r.user_id=? AND r.request_id=?""",
        (actor.company_id,actor.user_id,request_id))
    row = cursor.fetchone()
    if not row:
        return None
    result = dict(row)
    ats.application(cursor,actor,result.pop('application_id'))
    token = result.pop('token')
    if (result['status'] in ('waiting','created','opened') and
            datetime.fromisoformat(result['expires_at']) <= datetime.now(timezone.utc)):
        result['status'] = 'expired'
    return {**result, 'interview_id':token, 'interview_url':f'{actor.portal.origin}/interview/{token}'}


def save(root, actor, fields, filename, content, create_invite):
    request_id = fields.import_id.hex
    with closing(get_connection()) as connection:
        existing = receipt(connection.cursor(),actor,request_id)
    if existing:
        return existing

    name = ats.document_name(filename)
    text = ats_cv_import.read_cv(name,content)
    media = ats.document_media(name,content)
    digest = sha256(content).hexdigest()
    tenant = actor.company_id
    created_path = None
    try:
        with write(tenant) as cursor:
            existing = receipt(cursor,actor,request_id)
            if existing:
                return existing

            position_id = select_position(cursor,actor,fields)
            application = ats.record_application(cursor,tenant,position_id,fields,actor.user_id,
                'CV Analizi', allow_missing_email=True, reuse_application=True)
            app_id = application['id']
            # Check ownership before reading or attaching a document for an existing application.
            ats.application(cursor,actor,app_id)
            if not application.get('existing_application'):
                record(cursor,actor,'application_created',app_id)
            cursor.execute("""SELECT document_id FROM ats_document_digests
                WHERE company_id=? AND application_id=? AND digest=?""", (tenant,app_id,digest))
            document = cursor.fetchone()
            if document:
                document_id = document['document_id']
            else:
                document_id = ats.uid()
                path = ats.document_path(root,tenant,document_id).with_suffix(Path(name).suffix.lower())
                path.parent.mkdir(parents=True,exist_ok=True)
                with path.open('xb') as output:
                    created_path = path
                    output.write(content)
                ats.record_document(cursor,actor,app_id,document_id,name,media,content)

            invite = create_invite({
                'name':fields.name.strip(), 'email':fields.email.strip(), 'phone':fields.phone.strip(),
                'cv_text':text,
            }, actor, app_id, cv_document_id=document_id, active_cursor=cursor)
            cursor.execute('INSERT INTO ik_cv_invites VALUES (?,?,?,?,?)',
                           (tenant,actor.user_id,request_id,invite['interview_id'],invite['candidate_number']))
            return receipt(cursor,actor,request_id)
    except Exception:
        if created_path is not None:
            created_path.unlink(missing_ok=True)
        raise
