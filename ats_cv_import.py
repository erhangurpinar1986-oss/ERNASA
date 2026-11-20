import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field

import ats
from recruitment import position_name, record, staff_position, write
from services.cv_service import extract_contact_info, extract_text
from services.parser import is_cv_heading


logger = logging.getLogger(__name__)


class Selection(BaseModel):
    import_id: UUID
    position_id: str = Field(max_length=64)
    name: str = Field(max_length=160)
    email: str = Field(default='', max_length=254)
    phone: str = Field(default='', max_length=50)


def read_cv(filename, content):
    name = ats.document_name(filename)
    ats.document_media(name, content)
    try:
        with TemporaryDirectory() as directory:
            path = Path(directory) / ('cv' + Path(name).suffix.lower())
            path.write_bytes(content)
            text = extract_text(str(path)).strip()
    except Exception as error:
        logger.warning('ATS CV extraction failed (%s)', type(error).__name__)
        raise HTTPException(422, 'CV okunamadı.') from error
    if not text:
        raise HTTPException(422, 'CV içinde okunabilir metin bulunamadı.')
    return text


def is_missing_candidate_name(value):
    return not (value or '').strip() or is_cv_heading(value)


def preview(filename, content):
    contact = extract_contact_info(read_cv(filename, content))
    if is_missing_candidate_name(contact.get('name')):
        contact['name'] = ''
    return {'import_id': ats.uid(), **contact}


def save(root, actor, fields, filename, content, on_saved=None):
    name = ats.document_name(filename)
    tenant = actor.company_id
    document_id = ats.uid()
    path = ats.document_path(root, tenant, document_id).with_suffix(Path(name).suffix.lower())
    created = False
    try:
        with write(tenant) as cursor:
            cursor.execute("""SELECT 1 FROM ats_cv_imports WHERE company_id=? AND user_id=? AND import_id=?""",
                           (tenant, actor.user_id, fields.import_id.hex))
            if cursor.fetchone():
                return {'status': 'saved'}

            if is_missing_candidate_name(fields.name):
                raise HTTPException(422, 'Ad soyad\u0131 kontrol edin.')
            media = ats.document_media(name, content)
            if not fields.position_id:
                raise HTTPException(400, 'Pozisyon seçin.')
            position_name(cursor, tenant, fields.position_id)
            staff_position(cursor, actor, fields.position_id)
            result = ats.record_application(cursor, tenant, fields.position_id, fields, actor.user_id,
                                            'CV Yükleme', allow_missing_email=True)
            app_id = result['id']
            record(cursor, actor, 'application_created', app_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as output:
                created = True
                output.write(content)
            ats.record_document(cursor, actor, app_id, document_id, name, media, content)
            cursor.execute('INSERT INTO ats_cv_imports VALUES (?,?,?,?)',
                           (tenant, actor.user_id, fields.import_id.hex, document_id))
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise
    # Never report a successful automation when the post-import trigger failed.
    # The CV is already committed, so preserve it and surface a retryable state.
    automation_status = 'not_configured'
    if on_saved is not None:
        try:
            on_saved(actor, app_id, document_id)
            automation_status = 'triggered'
        except Exception as error:
            automation_status = 'failed'
            logger.exception('SELİN post-import callback failed; CV remains saved')
    return {'status': 'saved', 'automation_status': automation_status,
            'application_id': app_id, 'document_id': document_id}
