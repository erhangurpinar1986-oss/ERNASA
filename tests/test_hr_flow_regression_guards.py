"""Regression tests for nonsensical interview answers and bilingual ATS evidence.

Synthetic inputs only; no production records or network calls.
"""
from types import SimpleNamespace

from interview_assessments import insufficient_answer_evidence
from ats_matching import compare, parse_criteria


def _answers(text):
    return {i: {"answer": text} for i in range(1, 11)}


def test_keyboard_smashing_is_not_valid_interview_evidence():
    assert insufficient_answer_evidence(_answers("asdfghjklqwertyuiopzxcvbnm"))


def test_repeated_letters_are_not_valid_interview_evidence():
    assert insufficient_answer_evidence(_answers("aaaaaaaaaaaaaaaaaaaaaaaa"))


def test_meaningful_interview_answers_not_blocked():
    text = ("Müşterinin ihtiyacını dinlerim, seçenekleri açıklarım ve "
            "uygun çözümü sunarım. Sonucu takip edip ekibe bildiririm.")
    assert not insufficient_answer_evidence(_answers(text))


def test_english_cv_business_evidence_matches_turkish_criteria():
    criteria = parse_criteria("Mesleki deneyim | Kurumsal satış | 100 | kurumsal satis, musteri kazanimi")
    lines = [("20 years of experience in B2B sales, customer acquisition and account management.", 1)]
    result = compare(criteria, lines)
    assert result["requirements"][0]["status"] == "Eşleşti"
    assert result["evidence_coverage"] == 100


def test_random_cv_words_do_not_become_b2b_evidence():
    criteria = parse_criteria("Mesleki deneyim | Kurumsal satış | 100 | kurumsal satis")
    result = compare(criteria, [("asdfghjkl qwertyuiop zxcvbnm", 1)])
    assert result["match_score"] is None
    assert result["requirements"][0]["status"] == "CV’de kanıt bulunamadı"
