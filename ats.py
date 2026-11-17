import io
import logging
from base64 import b64decode, b64encode
from binascii import Error as Base64Error
import json
import re
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from database import get_connection
from recruitment import position_name, record, staff_position as require_staff_position, write
from services.parser import is_cv_heading


STAGES = ('new','screening','shortlist','interview','evaluation','offer','hired')
MAX_DOCUMENT = 10 * 1024 * 1024


def uid():
    return uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


class Applicant(BaseModel):
    name: str = Field(min_length=1,max_length=160)
    email: str = Field(min_length=3,max_length=254)
    phone: str = Field(default='',max_length=50)


class ApplicationCreate(Applicant):
    position_id: str = Field(min_length=1,max_length=64)
    source: str = Field(default='Manuel',max_length=80)
    tags: str = Field(default='',max_length=160)


class CandidateName(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(max_length=160)


class PublicApplication(Applicant):
    acknowledge: bool
    notice_version: str = Field(min_length=1,max_length=40)


class Job(BaseModel):
    position_id: str = Field(min_length=1,max_length=64)
    title: str = Field(min_length=1,max_length=160)
    summary: str = Field(min_length=1,max_length=6000)
    qualifications: str = Field(default='',max_length=6000)
    work_conditions: str = Field(default='',max_length=6000)
    state: Literal['draft','published','closed'] = 'draft'
    notice: str = Field(min_length=1,max_length=6000)
    notice_version: str = Field(min_length=1,max_length=40)
    confirm_publish: bool = False


class Note(BaseModel):
    note: str = Field(min_length=1,max_length=4000)


def author_clause(actor):
    if actor.role == 'hr_staff':
        return ' AND (a.created_by=? OR a.assigned_user_id=?)', (actor.user_id,actor.user_id)
    return '', ()


def application(cursor, actor, application_id):
    staff, params = author_clause(actor)
    cursor.execute(f"""SELECT a.*,c.name,c.email,c.phone,c.source,c.tags,p.name AS position
        FROM ats_applications a JOIN ats_candidates c ON c.id=a.candidate_id AND c.company_id=a.company_id
        JOIN company_positions p ON p.id=a.position_id AND p.company_id=a.company_id
        WHERE a.company_id=? AND a.id=? {staff}""", (actor.company_id,application_id,*params))
    result = cursor.fetchone()
    if result is None:
        raise HTTPException(404,'Başvuru bulunamadı.')
    return dict(result)


def list_applications(actor, search='', position_id='', stage='', limit=25, offset=0, assignee=''):
    staff, params = author_clause(actor)
    terms, args = [], [actor.company_id,*params]
    for sql, value in (('a.position_id=?',position_id),('a.stage=?',stage)):
        if value:
            terms.append(sql)
            args.append(value)
    if assignee == 'unassigned':
        terms.append('a.assigned_user_id IS NULL')
    elif assignee:
        if not assignee.isascii() or not assignee.isdecimal() or len(assignee)>18:
            raise HTTPException(400,'Geçersiz İK sorumlusu filtresi.')
        terms.append('a.assigned_user_id=?')
        args.append(int(assignee))
    if search:
        pattern = '%' + search.lower().replace('!','!!').replace('%','!%').replace('_','!_') + '%'
        terms.append("(lower(c.name) LIKE ? ESCAPE '!' OR lower(COALESCE(c.email,'')) LIKE ? ESCAPE '!')")
        args.extend((pattern,pattern))
    suffix = ''.join(' AND ' + term for term in terms)
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        query = f"""FROM ats_applications a JOIN ats_candidates c ON c.id=a.candidate_id AND c.company_id=a.company_id
            JOIN company_positions p ON p.id=a.position_id AND p.company_id=a.company_id
            WHERE a.company_id=? {staff} {suffix}"""
        cursor.execute('SELECT COUNT(*) AS total ' + query, args)
        total = cursor.fetchone()['total']
        cursor.execute("""SELECT a.*, c.name,c.email,c.phone,c.source,c.tags,p.name AS position,
            (SELECT COUNT(*) FROM interview_links i WHERE i.company_id=a.company_id
                AND i.application_id=a.id) AS interview_count """ + query +
            ' ORDER BY a.created_at DESC,a.id LIMIT ? OFFSET ?', (*args,limit,offset))
        return {'applications':[dict(row) for row in cursor.fetchall()],'total':total}


def record_application(cursor, tenant, position_id, details, creator, source, tags='', job_id=None,
                       *, allow_missing_email=False, reuse_application=False):
    name = validate_candidate_name(details.name)
    email = details.email.strip().lower()
    if (email or not allow_missing_email) and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):
        raise HTTPException(400,'Geçerli bir e-posta girin.')
    person = None
    if email:
        cursor.execute('SELECT id FROM ats_candidates WHERE company_id=? AND email=?',(tenant,email))
        person = cursor.fetchone()
    candidate_id = person['id'] if person else uid()
    if person:
        cursor.execute('SELECT id FROM ats_applications WHERE company_id=? AND candidate_id=? AND position_id=?',
                       (tenant,candidate_id,position_id))
        existing = cursor.fetchone()
        if existing:
            if reuse_application:
                return {'id':existing['id'],'candidate_id':candidate_id,'existing_candidate':True,
                        'existing_application':True}
            raise HTTPException(409,'Bu adayın pozisyon için başvurusu zaten var.')
    else:
        cursor.execute("""INSERT INTO ats_candidates(id,company_id,name,email,phone,source,tags,created_by,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",(candidate_id,tenant,name,email or None,details.phone.strip(),source,tags,creator,now()))
    app_id = uid()
    cursor.execute("""INSERT INTO ats_applications
        (id,company_id,candidate_id,position_id,job_id,stage,created_by,created_at,changed_at)
        VALUES (?,?,?,?,?,'new',?,?,?)""",(app_id,tenant,candidate_id,position_id,job_id,creator,now(),now()))
    return {'id':app_id,'candidate_id':candidate_id,'existing_candidate':bool(person)}


def create_application(actor, fields):
    with write(actor.company_id) as cursor:
        position_name(cursor, actor.company_id, fields.position_id)
        require_staff_position(cursor,actor,fields.position_id)
        result = record_application(cursor,actor.company_id,fields.position_id,fields,actor.user_id,fields.source.strip(),fields.tags.strip())
        record(cursor,actor,'application_created',result['id'])
        return result


def assign_application(actor, app_id, assignee):
    with write(actor.company_id) as cursor:
        application(cursor,actor,app_id)
        cursor.execute('UPDATE ats_applications SET assigned_user_id=?,revision=revision+1,changed_at=? WHERE id=? AND company_id=?',
                       (assignee,now(),app_id,actor.company_id))
        record(cursor,actor,'application_assigned',app_id)
    return {'success':True}


def validate_candidate_name(value):
    name = value.strip()
    if not name:
        raise HTTPException(400,'Adayın ad soyadını girin.')
    if is_cv_heading(name):
        raise HTTPException(400,'Özgeçmiş başlığı yerine adayın ad soyadını girin.')
    return name


def update_candidate_name(actor, app_id, fields):
    with write(actor.company_id) as cursor:
        row = application(cursor,actor,app_id)
        name = validate_candidate_name(fields.name)
        if row['name'] != name:
            cursor.execute('UPDATE ats_candidates SET name=? WHERE company_id=? AND id=?',
                           (name,actor.company_id,row['candidate_id']))
            record(cursor,actor,'candidate_name_updated',row['candidate_id'])
    return {'name':name}


def set_position_member(actor, position_id, user_id, enabled):
    with write(actor.company_id) as cursor:
        cursor.execute('SELECT 1 FROM company_positions WHERE id=? AND company_id=?',(position_id,actor.company_id))
        if not cursor.fetchone():
            raise HTTPException(404,'Pozisyon bulunamadı.')
        if enabled:
            cursor.execute('INSERT INTO ats_position_members VALUES (?,?,?) ON CONFLICT DO NOTHING',
                           (actor.company_id,position_id,user_id))
        else:
            cursor.execute('DELETE FROM ats_position_members WHERE company_id=? AND position_id=? AND user_id=?',
                           (actor.company_id,position_id,user_id))
        if cursor.rowcount:
            cursor.execute('DELETE FROM ik_portal_sessions WHERE company_id=? AND user_id=?',
                           (actor.company_id,user_id))
        record(cursor,actor,'position_member_changed',position_id)
    return {'success':True}


def position_members(actor, position_id):
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute('SELECT 1 FROM company_positions WHERE id=? AND company_id=?',(position_id,actor.company_id))
        if not cursor.fetchone():
            raise HTTPException(404,'Pozisyon bulunamadı.')
        cursor.execute('SELECT user_id FROM ats_position_members WHERE company_id=? AND position_id=?',
                       (actor.company_id,position_id))
        return [row['user_id'] for row in cursor.fetchall()]


def list_jobs(actor):
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute("""SELECT j.*,COALESCE(d.qualifications,'') AS qualifications,COALESCE(d.work_conditions,'') AS work_conditions
            FROM ats_jobs j LEFT JOIN ats_job_details d ON d.job_id=j.id AND d.company_id=j.company_id
            WHERE j.company_id=? ORDER BY j.created_at DESC""",(actor.company_id,))
        return [dict(row) for row in cursor.fetchall()]


def save_job(actor, fields):
    if fields.state == 'published' and not fields.confirm_publish:
        raise HTTPException(400,'Yerel ilan yayını açıkça onaylanmalıdır.')
    with write(actor.company_id) as cursor:
        if fields.state == 'published':
            position_name(cursor,actor.company_id,fields.position_id)
        else:
            cursor.execute('SELECT 1 FROM company_positions WHERE id=? AND company_id=?',(fields.position_id,actor.company_id))
            if not cursor.fetchone():
                raise HTTPException(404,'Pozisyon bulunamadı.')
        cursor.execute('SELECT id FROM ats_jobs WHERE company_id=? AND position_id=?',(actor.company_id,fields.position_id))
        old = cursor.fetchone()
        job_id = old['id'] if old else uid()+uid()
        title, summary, notice, version = (str(value).strip() for value in
                                          (fields.title,fields.summary,fields.notice,fields.notice_version))
        if not all((title,summary,notice,version)):
            raise HTTPException(400,'İlan ve aydınlatma bilgileri zorunludur.')
        cursor.execute("""INSERT INTO ats_jobs VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(company_id,position_id) DO UPDATE SET title=excluded.title,summary=excluded.summary,
            state=excluded.state,notice=excluded.notice,notice_version=excluded.notice_version""",
            (job_id,actor.company_id,fields.position_id,title,summary,fields.state,notice,version,now()))
        cursor.execute("""INSERT INTO ats_job_details VALUES(?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET qualifications=excluded.qualifications,work_conditions=excluded.work_conditions""",
            (job_id,actor.company_id,fields.qualifications.strip(),fields.work_conditions.strip()))
        record(cursor,actor,'job_'+fields.state,job_id)
    return {'id':job_id,'application_path':'/basvuru/'+job_id if fields.state=='published' else None}


def public_job(cursor, company_id, job_id):
    cursor.execute("""SELECT j.id,j.position_id,j.title,j.summary,j.notice,j.notice_version,p.name AS position,
        COALESCE(d.qualifications,'') AS qualifications,COALESCE(d.work_conditions,'') AS work_conditions
        FROM ats_jobs j JOIN company_positions p ON p.id=j.position_id AND p.company_id=j.company_id
        LEFT JOIN ats_job_details d ON d.job_id=j.id AND d.company_id=j.company_id
        WHERE j.id=? AND j.company_id=? AND j.state='published' AND p.open=1""",(job_id,company_id))
    job = cursor.fetchone()
    if not job:
        raise HTTPException(404,'Başvuru bağlantısı bulunamadı.')
    return dict(job)


def public_application(tenant, job_id, fields):
    with write(tenant) as cursor:
        job = public_job(cursor,tenant,job_id)
        if not fields.acknowledge or fields.notice_version != job['notice_version']:
            raise HTTPException(400,'Güncel aday aydınlatma metnini okuyup onaylayın.')
        result = record_application(cursor,tenant,job['position_id'],fields,None,'İlan',job_id=job_id)
        cursor.execute('INSERT INTO ats_notices VALUES (?,?,?,?,?)',
                       (result['id'],tenant,job['notice'],job['notice_version'],now()))
    return {'success':True}


def details(actor, app_id):
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        selected = application(cursor,actor,app_id)
        result = {'application':selected}
        for key, table, order in (
            ('notes','ats_notes','created_at'),('history','ats_stage_events','occurred_at'),
            ('documents','ats_documents','created_at'),
        ):
            cursor.execute(f'SELECT * FROM {table} WHERE company_id=? AND application_id=? ORDER BY {order}',
                           (actor.company_id,app_id))
            result[key] = [dict(row) for row in cursor.fetchall()]
        if 'ai' in actor.products:
            cursor.execute("""SELECT token,status,created_at FROM interview_links WHERE company_id=? AND application_id=?
                ORDER BY created_at DESC""",(actor.company_id,app_id))
            result['interviews'] = [dict(row) for row in cursor.fetchall()]
        return result


def add_note(actor, app_id, fields):
    value = fields.note.strip()
    if not value:
        raise HTTPException(400,'Not boş olamaz.')
    note_id = uid()
    with write(actor.company_id) as cursor:
        application(cursor,actor,app_id)
        cursor.execute('INSERT INTO ats_notes VALUES (?,?,?,?,?,?)',
                       (note_id,actor.company_id,app_id,actor.user_id,value,now()))
        record(cursor,actor,'application_note_added',app_id)
    return {'id':note_id}


def document_media(filename, content):
    suffix = Path(filename).suffix.lower()
    if 0 < len(content) <= MAX_DOCUMENT:
        if suffix == '.pdf' and content.startswith(b'%PDF-'):
            return 'application/pdf'
        if suffix == '.docx' and zipfile.is_zipfile(io.BytesIO(content)):
            with zipfile.ZipFile(io.BytesIO(content)) as container:
                members = container.infolist()
                if len(members) <= 500 and sum(item.file_size for item in members) <= 40 * 1024 * 1024:
                    if '[Content_Types].xml' in container.namelist() and 'word/document.xml' in container.namelist():
                        return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    raise HTTPException(400,'En fazla 10 MB geçerli PDF veya DOCX yükleyin.')


def document_path(root, tenant, document_id):
    return root / 'companies' / str(tenant) / 'ats' / document_id


def document_name(filename):
    path = Path(Path(filename.replace('\\', '/')).name)
    if path.suffix.lower() in ('.pdf', '.docx'):
        return path.stem[:200 - len(path.suffix)] + path.suffix
    return path.name[:200]


def archive_document(cursor, actor, app_id, document_id, content):
    """Store CV bytes additively in the existing database for restart-safe retrieval."""
    cursor.execute("""INSERT INTO ats_document_blobs
        (document_id,company_id,application_id,digest,content_base64)
        VALUES (?,?,?,?,?) ON CONFLICT(document_id) DO NOTHING""",
        (document_id,actor.company_id,app_id,sha256(content).hexdigest(),
         b64encode(content).decode('ascii')))


def record_document(cursor, actor, app_id, document_id, name, media, content):
    cursor.execute('INSERT INTO ats_documents VALUES (?,?,?,?,?,?,?,?)',
                   (document_id,actor.company_id,app_id,name,media,len(content),actor.user_id,now()))
    cursor.execute('INSERT INTO ats_document_digests VALUES (?,?,?,?)',
                   (actor.company_id,app_id,sha256(content).hexdigest(),document_id))
    archive_document(cursor,actor,app_id,document_id,content)
    record(cursor,actor,'application_document_added',app_id)


def upload_document(root, actor, app_id, filename, content, job_id=None):
    name = Path(filename).name[:200]
    document_id = job_id or uid()
    path = document_path(root,actor.company_id,document_id).with_suffix(Path(name).suffix.lower())
    created = False
    restored = False
    try:
        with write(actor.company_id) as cursor:
            if job_id:
                cursor.execute("""SELECT status FROM ats_upload_jobs
                    WHERE id=? AND company_id=? AND application_id=? AND user_id=?""",
                    (job_id,actor.company_id,app_id,actor.user_id))
                job = cursor.fetchone()
                if not job or job['status'] != 'pending':
                    return None
            application(cursor,actor,app_id)
            media = document_media(name,content)
            digest = sha256(content).hexdigest()
            cursor.execute("""SELECT d.document_id,v.filename FROM ats_document_digests d
                JOIN ats_documents v ON v.id=d.document_id AND v.company_id=d.company_id
                    AND v.application_id=d.application_id
                WHERE d.company_id=? AND d.application_id=? AND d.digest=?""",
                (actor.company_id,app_id,digest))
            existing = cursor.fetchone()
            if existing:
                # An old CV record survives a deploy even when its local file does not.
                # Reattach identical content to its ORIGINAL document ID; never duplicate
                # or overwrite the existing candidate/application/interview records.
                document_id = existing['document_id']
                old_path = document_path(root,actor.company_id,document_id)
                path = old_path.with_suffix(Path(existing['filename']).suffix.lower())
                if path.is_file() or old_path.is_file():
                    raise HTTPException(409,'Bu dosya bu başvuruya zaten yüklenmiş.')
                path.parent.mkdir(parents=True,exist_ok=True)
                try:
                    with path.open('xb') as output:
                        created = True
                        restored = True
                        output.write(content)
                except FileExistsError:
                    raise HTTPException(409,'Bu dosya bu başvuruya zaten yüklenmiş.') from None
                archive_document(cursor,actor,app_id,document_id,content)
                record(cursor,actor,'application_document_restored',app_id)
                if job_id:
                    cursor.execute("UPDATE ats_upload_jobs SET status='ready',payload=NULL,error=NULL WHERE id=?", (job_id,))
                return {'id':document_id,'restored':True}
            path.parent.mkdir(parents=True,exist_ok=True)
            # The tenant lock protects replacement of a file left by an interrupted pending job.
            with path.open('wb' if job_id else 'xb') as output:
                created = True
                output.write(content)
            record_document(cursor,actor,app_id,document_id,name,media,content)
            if job_id:
                cursor.execute("UPDATE ats_upload_jobs SET status='ready',payload=NULL,error=NULL WHERE id=?", (job_id,))
    except Exception:
        # Rollback cleanup touches ONLY the file created by this request, not old CVs.
        if created and (not job_id or restored):
            path.unlink(missing_ok=True)
        raise
    return {'id':document_id}


def get_document(root, actor, app_id, document_id):
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        application(cursor,actor,app_id)
        cursor.execute("""SELECT d.filename,d.media_type,d.size_bytes,
                b.content_base64,b.digest AS blob_digest
            FROM ats_documents d
            LEFT JOIN ats_document_blobs b ON b.document_id=d.id AND b.company_id=d.company_id
                AND b.application_id=d.application_id
            WHERE d.id=? AND d.company_id=? AND d.application_id=?""",
            (document_id,actor.company_id,app_id))
        result = cursor.fetchone()
    if not result:
        raise HTTPException(404,'Belge kaydı bulunamadı.')
    old_path = document_path(root,actor.company_id,document_id)
    path = old_path.with_suffix(Path(result['filename']).suffix.lower())
    if not path.is_file() and old_path.is_file():
        path = old_path
    if not path.is_file():
        encoded = result['content_base64']
        if not encoded:
            raise HTTPException(404,'CV kaydı korunuyor ancak dosya bulunamadı. Orijinal CV\'yi aynı başvuruya yeniden yükleyerek belgeyi onarın.')
        try:
            payload = b64decode(encoded,validate=True)
        except (Base64Error,ValueError):
            raise HTTPException(503,'CV arşivi doğrulanamadı; destek ekibine başvurun.') from None
        if len(payload) != result['size_bytes'] or sha256(payload).hexdigest() != result['blob_digest']:
            raise HTTPException(503,'CV arşivi bütünlük kontrolünden geçemedi; destek ekibine başvurun.')
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with path.open('xb') as output:
                output.write(payload)
        except FileExistsError:
            pass  # Another authenticated request restored the same document.
    else:
        # Opportunistically back up legacy files while they are still accessible.
        if not result['content_base64']:
            try:
                payload = path.read_bytes()
                if 0 < len(payload) <= MAX_DOCUMENT and len(payload) == result['size_bytes']:
                    with write(actor.company_id) as cursor:
                        cursor.execute("""SELECT digest FROM ats_document_digests
                            WHERE company_id=? AND application_id=? AND document_id=?""",
                            (actor.company_id,app_id,document_id))
                        row = cursor.fetchone()
                        if row and row['digest'] == sha256(payload).hexdigest():
                            archive_document(cursor,actor,app_id,document_id,payload)
            except (OSError,RuntimeError) as error:
                # Never prevent an existing CV from opening because archival failed.
                logging.getLogger(__name__).warning('ATS legacy CV archival failed: %s',type(error).__name__)
    if not path.is_file():
        raise HTTPException(503,'CV dosyası depodan geri yüklenemedi.')
    return path, result['filename'], result['media_type']
