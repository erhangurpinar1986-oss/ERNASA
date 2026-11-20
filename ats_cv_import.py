import json
import logging
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field

import ats
import ats_matching
import hr_flow_screening
from database import get_connection
from recruitment import positions
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


def preview(filename, content, actor=None):
    text = read_cv(filename, content)
    contact = extract_contact_info(text)
    if is_missing_candidate_name(contact.get('name')):
        contact['name'] = ''
    sections = {"education": [], "experience": []}
    section = None
    for line in (line.strip() for line in text.splitlines() if line.strip()):
        heading, _, inline = line.partition(":")
        key = ats_matching.plain(heading).strip()
        if key in ("egitim", "egitim bilgileri", "education", "akademik gecmis"):
            section = "education"
        elif key in ("deneyim", "is deneyimi", "is tecrubesi", "mesleki deneyim", "experience", "work experience"):
            section = "experience"
        elif key in ("beceriler", "yetenekler", "referanslar", "sertifikalar", "iletisim", "skills", "references", "languages"):
            section = None
        else:
            if section and len(sections[section]) < 12:
                sections[section].append(line[:250])
            continue
        if section and inline.strip():
            sections[section].append(inline.strip()[:250])
    options = [p for p in positions(actor) if p["open"]] if actor else []
    lines = [(line, None) for line in ats_matching.relevant_text(text).splitlines() if line.strip()]
    ranked = []
    for option in options:
        try:
            criteria = ats_matching.parse_criteria(option.get("matching_criteria") or "")
        except HTTPException:
            continue
        result = ats_matching.compare(criteria, lines)
        score, coverage = result["match_score"], result["evidence_coverage"]
        if score is not None and coverage and score > 0:
            ranked.append((score * coverage, option["id"]))
    ranked.sort(reverse=True)
    suggestion = options[0]["id"] if len(options) == 1 else None
    if ranked and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
        suggestion = ranked[0][1]
    name_parts = contact['name'].rsplit(maxsplit=1)
    return {'import_id': ats.uid(), **contact, **sections,
            'first_name': name_parts[0] if name_parts else '',
            'last_name': name_parts[1] if len(name_parts) == 2 else '',
            'suggested_position_id': suggestion}


def screening_status(actor, application_id, document_id):
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        ats.application(cursor, actor, application_id)
        cursor.execute(f"""SELECT s.analysis_state,s.match_score,s.coverage,s.details_json,
                {ats.ATS_CLASSIFICATION} AS classification
            FROM ats_documents d LEFT JOIN flow_screening_results s
                ON s.company_id=d.company_id AND s.application_id=d.application_id AND s.document_id=d.id
            WHERE d.company_id=? AND d.application_id=? AND d.id=?""",
            (actor.company_id, application_id, document_id))
        row = cursor.fetchone()
        permission = hr_flow_screening.interview_permission(cursor, actor.company_id, application_id)
    if not row:
        raise HTTPException(404, 'CV bulunamadı.')
    data = json.loads(row['details_json'] or '{}')
    criteria = data.get('requirements', [])
    return {'state': row['analysis_state'] or 'pending', 'score': row['match_score'],
            'coverage': row['coverage'], 'classification':row['classification'], 'permission':permission, 'reason': data.get('reason'),
            'strengths': [item for item in criteria if item['status'] == 'Eşleşti'],
            'development': [item for item in criteria if item['status'] != 'Eşleşti']}


def save(root, actor, fields, filename, content, on_saved=None):
    name = ats.document_name(filename)
    tenant = actor.company_id
    document_id = ats.uid()
    path = ats.document_path(root, tenant, document_id).with_suffix(Path(name).suffix.lower())
    created = False
    try:
        with write(tenant) as cursor:
            cursor.execute("""SELECT i.document_id,d.application_id FROM ats_cv_imports i
                JOIN ats_documents d ON d.id=i.document_id AND d.company_id=i.company_id
                WHERE i.company_id=? AND i.user_id=? AND i.import_id=?""",
                           (tenant, actor.user_id, fields.import_id.hex))
            prior = cursor.fetchone()
            if prior:
                ats.application(cursor, actor, prior['application_id'])
                return {'status': 'saved', **dict(prior)}

            if is_missing_candidate_name(fields.name):
                raise HTTPException(422, 'Ad soyad\u0131 kontrol edin.')
            media = ats.document_media(name, content)
            if not fields.position_id:
                raise HTTPException(400, 'Pozisyon seçin.')
            position_name(cursor, tenant, fields.position_id)
            staff_position(cursor, actor, fields.position_id)
            result = ats.record_application(cursor, tenant, fields.position_id, fields, actor.user_id,
                                            'CV Yükleme', allow_missing_email=True, reuse_application=True,
                                            review_identity_conflict=True)
            app_id = result['id']
            if result.get('existing_application'):
                ats.application(cursor, actor, app_id)
                cursor.execute("""SELECT document_id FROM ats_document_digests
                    WHERE company_id=? AND application_id=? AND digest=?""",
                    (tenant, app_id, sha256(content).hexdigest()))
                existing = cursor.fetchone()
                if existing:
                    ats.queue_screening(cursor, actor, app_id, existing['document_id'])
                    return {'status':'saved', 'application_id':app_id, 'document_id':existing['document_id'],
                            'existing_application':True, 'identity_conflict':result.get('identity_conflict',False)}
            else:
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
            logger.warning('SELİN post-import callback failed (%s)',type(error).__name__)
    return {'status': 'saved', 'automation_status': automation_status,
            'application_id': app_id, 'document_id': document_id,
            'existing_application':result.get('existing_application',False),
            'identity_conflict':result.get('identity_conflict',False)}
