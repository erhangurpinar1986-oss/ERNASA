"""Gerçek SQLite ATS başvurusundan CV eşleşmesine kadar sentetik entegrasyon testi."""
from types import SimpleNamespace
import pytest
from fastapi import HTTPException

import ats
import ats_matching
import database
import recruitment


@pytest.fixture()
def sample_ats(tmp_path, monkeypatch):
    import pymupdf

    monkeypatch.setattr(database, "DATABASE_URL", None)
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "arn_test.sqlite")
    database.initialize_database()

    actor = SimpleNamespace(company_id=501, user_id=7, role="hr_manager", products=("ats",))
    department = recruitment.add_department(actor, "Lojistik")
    position = recruitment.save_position(actor, recruitment.Position(
        name="Depo Personeli", department_id=department["id"],
        matching_criteria=(
            "Yetkinlik | WMS deneyimi | 60 | wms\n"
            "Sertifika | Forklift tecrubesi | 40 | forklift"
        ),
    ))
    application = ats.create_application(actor, ats.ApplicationCreate(
        name="Sentetik Aday", email="sample-ats@example.org",
        position_id=position["id"],
    ))
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((70, 100), "WMS ve forklift tecrubem var.")
    payload = pdf.tobytes()
    pdf.close()
    root = tmp_path / "uploads"
    document = ats.upload_document(root, actor, application["id"], "sentetik_cv.pdf", payload)
    return root, actor, position["id"], application["id"], document["id"]


def test_saved_position_criteria_reach_uploaded_cv(sample_ats):
    root, actor, _, app_id, doc_id = sample_ats
    comparison = ats_matching.evidence_match(root, actor, app_id, doc_id)
    assert comparison["match_score"] == 100
    assert comparison["evidence_coverage"] == 100
    assert comparison["human_review_required"] is True
    assert len(comparison["requirements"]) == 2


def test_position_criteria_revised_without_cv_reupload(sample_ats):
    root, actor, position_id, app_id, doc_id = sample_ats
    positions = recruitment.positions(actor)
    saved = next(item for item in positions if item["id"] == position_id)
    assert saved["matching_criteria"]
    recruitment.save_position(actor, recruitment.Position(
        name=saved["name"], department_id=saved["department_id"],
        matching_criteria="Yetkinlik | WMS deneyimi | 100 | wms",
    ), position_id=position_id)
    comparison = ats_matching.evidence_match(root, actor, app_id, doc_id)
    assert comparison["match_score"] == 100
    assert len(comparison["requirements"]) == 1


def test_unrelated_company_cannot_read_another_company_cv(sample_ats):
    root, actor, _, app_id, doc_id = sample_ats
    other = SimpleNamespace(company_id=502, user_id=8, role="hr_manager", products=("ats",))
    with pytest.raises(HTTPException) as error:
        ats_matching.evidence_match(root, other, app_id, doc_id)
    assert error.value.status_code == 404


def test_cv_survives_ephemeral_disk_loss_using_durable_archive(sample_ats):
    root, actor, _, app_id, doc_id = sample_ats
    path, _, _ = ats.get_document(root, actor, app_id, doc_id)
    original = path.read_bytes()
    path.unlink()
    restored, _, _ = ats.get_document(root, actor, app_id, doc_id)
    assert restored.read_bytes() == original
    assert ats_matching.evidence_match(root, actor, app_id, doc_id)["match_score"] == 100


def test_legacy_cv_reupload_restores_original_record_without_duplicates(sample_ats):
    root, actor, _, app_id, doc_id = sample_ats
    path, _, _ = ats.get_document(root, actor, app_id, doc_id)
    original = path.read_bytes()
    with recruitment.write(actor.company_id) as cursor:
        cursor.execute("DELETE FROM ats_document_blobs WHERE document_id=?", (doc_id,))
    path.unlink()
    with pytest.raises(HTTPException) as missing:
        ats.get_document(root, actor, app_id, doc_id)
    assert missing.value.status_code == 404
    result = ats.upload_document(root, actor, app_id, "sentetik_cv.pdf", original)
    assert result == {"id": doc_id, "restored": True}
    recovered, _, _ = ats.get_document(root, actor, app_id, doc_id)
    assert recovered.read_bytes() == original
    assert len(ats.details(actor, app_id)["documents"]) == 1
    with recruitment.write(actor.company_id) as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM ats_document_blobs WHERE document_id=?", (doc_id,))
        assert cursor.fetchone()["n"] == 1


def test_duplicate_cv_does_not_replace_existing_record(sample_ats):
    root, actor, _, app_id, doc_id = sample_ats
    path, _, _ = ats.get_document(root, actor, app_id, doc_id)
    with pytest.raises(HTTPException) as duplicate:
        ats.upload_document(root, actor, app_id, "sentetik_cv.pdf", path.read_bytes())
    assert duplicate.value.status_code == 409
    assert len(ats.details(actor, app_id)["documents"]) == 1


def test_recovery_via_existing_ats_upload_queue_preserves_identity(sample_ats, monkeypatch):
    from types import SimpleNamespace
    import ats_upload_queue
    import control_integration

    root, actor, _, app_id, doc_id = sample_ats
    path, _, _ = ats.get_document(root, actor, app_id, doc_id)
    original = path.read_bytes()
    with recruitment.write(actor.company_id) as cursor:
        cursor.execute("DELETE FROM ats_document_blobs WHERE document_id=?", (doc_id,))
    path.unlink()
    uploader = SimpleNamespace(company_id=actor.company_id, user_id=actor.user_id,
        role="hr_manager", products=("ats",),
        portal=control_integration.Portal("test-company",""),
        access_version="test-revision")
    queued = ats_upload_queue.enqueue(uploader, app_id, "sentetik_cv.pdf", original)
    assert queued["status"] == "pending"
    monkeypatch.setattr(ats_upload_queue.control,"control_call", lambda *_args,**_kwargs: {
        "company_id":actor.company_id,"version":"test-revision",
        "role":"hr_manager","must_change_password":False,"products":["ats"]})
    assert ats_upload_queue.process_one(root)
    statuses = ats_upload_queue.jobs(uploader,app_id)
    assert next(row for row in statuses if row["id"] == queued["id"])["status"] == "ready"
    assert ats.get_document(root, actor, app_id, doc_id)[0].read_bytes() == original
    assert len(ats.details(actor, app_id)["documents"]) == 1


def test_corrupt_archive_fails_closed_without_creating_a_cv(sample_ats):
    root, actor, _, app_id, doc_id = sample_ats
    path, _, _ = ats.get_document(root, actor, app_id, doc_id)
    with recruitment.write(actor.company_id) as cursor:
        cursor.execute("UPDATE ats_document_blobs SET content_base64=? WHERE document_id=?",
                       ("NOT-VALID-BASE64",doc_id))
    path.unlink()
    with pytest.raises(HTTPException) as failure:
        ats.get_document(root, actor, app_id, doc_id)
    assert failure.value.status_code == 503
    assert not path.exists()
    assert len(ats.details(actor, app_id)["documents"]) == 1


def _attach_synthetic_interview_text(actor, app_id, doc_id, text):
    with recruitment.write(actor.company_id) as cursor:
        token = ats.uid()
        cursor.execute("""INSERT INTO interview_links
            (token,name,company,position,created_at,status,cv_text,company_id,application_id)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (token,"Sentetik Aday","Test Firmasi","Depo Personeli",ats.now(),
             "completed",text,actor.company_id,app_id))
        cursor.execute("INSERT INTO ats_interview_cv_sources VALUES (?,?,?)",
                       (token,actor.company_id,doc_id))
    return token


def test_missing_original_uses_exact_interview_cv_text_for_matching(sample_ats):
    root, actor, _, app_id, doc_id = sample_ats
    _attach_synthetic_interview_text(actor,app_id,doc_id,
                                     "Yetkinlikler: WMS ve forklift tecrubem var")
    path, _, _ = ats.get_document(root,actor,app_id,doc_id)
    with recruitment.write(actor.company_id) as cursor:
        cursor.execute("DELETE FROM ats_document_blobs WHERE document_id=?", (doc_id,))
    path.unlink()
    recovered = ats_matching.evidence_match(root,actor,app_id,doc_id)
    assert recovered["match_score"] == 100
    assert recovered["source"] == "stored_interview_cv_text"
    assert recovered["original_file_available"] is False
    assert "WMS" in ats_matching.source_text(root,actor,app_id,doc_id)
    assert not path.exists()  # A text-only fallback never fabricates the original PDF.


def test_missing_original_without_exact_document_link_does_not_guess(sample_ats):
    root, actor, _, app_id, doc_id = sample_ats
    with recruitment.write(actor.company_id) as cursor:
        token = ats.uid()
        cursor.execute("""INSERT INTO interview_links
            (token,name,company,position,created_at,status,cv_text,company_id,application_id)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (token,"Sentetik Aday","Test Firmasi","Depo Personeli",ats.now(),
             "completed","WMS forklift",actor.company_id,app_id))
        cursor.execute("DELETE FROM ats_document_blobs WHERE document_id=?", (doc_id,))
    path, _, _ = (ats.document_path(root,actor.company_id,doc_id).with_suffix(".pdf"),None,None)
    path.unlink()
    with pytest.raises(HTTPException) as error:
        ats_matching.evidence_match(root,actor,app_id,doc_id)
    assert error.value.status_code == 404


def test_stored_interview_text_is_tenant_scoped(sample_ats):
    root, actor, _, app_id, doc_id = sample_ats
    _attach_synthetic_interview_text(actor,app_id,doc_id,"WMS forklift")
    other = SimpleNamespace(company_id=999,user_id=20,role="hr_manager",products=("ats",))
    with pytest.raises(HTTPException) as error:
        ats_matching.stored_cv_text(other,app_id,doc_id)
    assert error.value.status_code == 404


def test_document_availability_reports_verified_and_missing_without_mutation(sample_ats):
    root,actor,_,app_id,doc_id = sample_ats
    original = ats.document_availability(root,actor,app_id)["documents"]
    assert len(original) == 1
    assert original[0]["id"] == doc_id
    assert original[0]["status"] == "disk_verified"
    file_path,_,_ = ats.get_document(root,actor,app_id,doc_id)
    _attach_synthetic_interview_text(actor,app_id,doc_id,"WMS forklift")
    with recruitment.write(actor.company_id) as cursor:
        cursor.execute("DELETE FROM ats_document_blobs WHERE document_id=?",(doc_id,))
    file_path.unlink()
    recovered = ats.document_availability(root,actor,app_id)["documents"]
    assert len(recovered) == 1
    assert recovered[0]["status"] == "text_only"
    assert recovered[0]["stored_interview_text_available"] is True
    assert not file_path.exists()


def test_document_availability_is_tenant_scoped(sample_ats):
    root, actor, _, app_id, doc_id = sample_ats
    other = SimpleNamespace(company_id=999,user_id=20,role="hr_manager",products=("ats",))
    with pytest.raises(HTTPException) as err:
        ats.document_availability(root,other,app_id)
    assert err.value.status_code == 404


def test_reupload_same_cv_keeps_completed_interview_answers_report_and_link(sample_ats):
    """Regression: restore a lost original without touching an already finished interview."""
    root, actor, _, app_id, doc_id = sample_ats
    token = _attach_synthetic_interview_text(actor,app_id,doc_id,
                                             "WMS forklift tecrubem var")
    with recruitment.write(actor.company_id) as cursor:
        cursor.execute("""INSERT INTO interview_answers
            (token,question_number,question,answer,created_at)
            VALUES (?,?,?,?,?)""",
            (token,1,"Kendinizi tanitin.","Sentetik mulakat yaniti",ats.now()))
        cursor.execute("""INSERT INTO interview_reports (token,job_fit_analysis,created_at)
            VALUES (?,?,?)""",
            (token,"Sentetik degerlendirme raporu",ats.now()))
        cursor.execute("SELECT token,status,application_id,cv_text FROM interview_links WHERE token=?",(token,))
        link_before = dict(cursor.fetchone())
        cursor.execute("SELECT token,question_number,question,answer,created_at FROM interview_answers WHERE token=?",(token,))
        answers_before = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT token,job_fit_analysis,created_at FROM interview_reports WHERE token=?",(token,))
        report_before = dict(cursor.fetchone())
    path, _, _ = ats.get_document(root,actor,app_id,doc_id)
    original_bytes = path.read_bytes()
    with recruitment.write(actor.company_id) as cursor:
        cursor.execute("DELETE FROM ats_document_blobs WHERE document_id=?",(doc_id,))
    path.unlink()  # Only test fixture: imitate Render ephemeral disk loss.
    restored = ats.upload_document(root,actor,app_id,"sentetik_cv.pdf",original_bytes)
    assert restored == {"id":doc_id,"restored":True}
    with recruitment.write(actor.company_id) as cursor:
        cursor.execute("SELECT token,status,application_id,cv_text FROM interview_links WHERE token=?",(token,))
        assert dict(cursor.fetchone()) == link_before
        cursor.execute("SELECT token,question_number,question,answer,created_at FROM interview_answers WHERE token=?",(token,))
        assert [dict(row) for row in cursor.fetchall()] == answers_before
        cursor.execute("SELECT token,job_fit_analysis,created_at FROM interview_reports WHERE token=?",(token,))
        assert dict(cursor.fetchone()) == report_before
        cursor.execute("SELECT token,company_id,document_id FROM ats_interview_cv_sources WHERE token=?",(token,))
        source = dict(cursor.fetchone())
        assert source["document_id"] == doc_id
        assert source["company_id"] == actor.company_id
    assert len(ats.details(actor,app_id)["documents"]) == 1
    assert ats.get_document(root,actor,app_id,doc_id)[0].read_bytes() == original_bytes


def test_image_only_pdf_uses_offline_ocr_for_cv_matching(sample_ats,monkeypatch):
    root,actor,_,app_id,doc_id=sample_ats
    import ats_matching
    original=ats_matching._pdf_text_lines
    # A scanned PDF has no legible native text; bounded offline OCR supplies lines.
    monkeypatch.setattr(ats_matching,"_pdf_text_lines",lambda path: [])
    monkeypatch.setattr(ats_matching,"_ocr_pdf_lines",
                        lambda path: [("WMS ve forklift tecrubem var",1)])
    match=ats_matching.evidence_match(root,actor,app_id,doc_id)
    assert match["match_score"] == 100
    assert match["evidence_coverage"] == 100
    assert match["text_extraction_status"] == "readable"
    assert "WMS" in ats_matching.source_text(root,actor,app_id,doc_id)
    monkeypatch.setattr(ats_matching,"_pdf_text_lines",original)


def test_unreadable_cv_is_not_scored_as_zero(sample_ats,monkeypatch):
    root,actor,_,app_id,doc_id=sample_ats
    import ats_matching
    monkeypatch.setattr(ats_matching,"_pdf_text_lines",lambda path: [])
    monkeypatch.setattr(ats_matching,"_ocr_pdf_lines",lambda path: [])
    comparison=ats_matching.evidence_match(root,actor,app_id,doc_id)
    assert comparison["match_score"] is None
    assert comparison["evidence_coverage"] == 0
    assert comparison["text_extraction_status"] == "unreadable"
    assert "OCR" in comparison["reason"]
