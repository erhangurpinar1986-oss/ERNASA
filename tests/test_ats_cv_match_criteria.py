"""CV–pozisyon eşleşme kriterleri ve kanıt kapsamı regresyon testleri."""
import pytest
from fastapi import HTTPException

from ats_matching import compare, parse_criteria


def test_position_matching_criteria_parse_and_weighted_full_match():
    criteria = parse_criteria(
        "Yetkinlik | Stok yönetimi | 60 | stok yönetimi\n"
        "Sertifika | Forklift sertifikası | 40 | forklift sertifikası"
    )
    assert len(criteria) == 2
    result = compare(criteria, [
        ("Stok yönetimi deneyimim var.", 1),
        ("Forklift sertifikası eğitimini tamamladım.", 2),
    ])
    assert result["match_score"] == 100
    assert result["evidence_coverage"] == 100
    assert result["human_review_required"] is True


def test_missing_cv_evidence_is_not_a_zero_score():
    criteria = parse_criteria(
        "Yetkinlik | Stok yönetimi | 60 | stok yönetimi\n"
        "Sertifika | Forklift sertifikası | 40 | forklift sertifikası"
    )
    result = compare(criteria, [("Stok yönetimi deneyimim var.", 1)])
    assert result["match_score"] == 100
    assert result["evidence_coverage"] == 60
    assert result["requirements"][1]["status"] == "CV’de kanıt bulunamadı"


def test_empty_criteria_have_no_percentage():
    result = compare([], [])
    assert result["match_score"] is None
    assert result["evidence_coverage"] is None


def test_invalid_criteria_are_rejected():
    with pytest.raises(HTTPException) as error:
        parse_criteria("Yetkinlik | Stok yönetimi | 0 | stok yönetimi")
    assert error.value.status_code == 400
