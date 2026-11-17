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
