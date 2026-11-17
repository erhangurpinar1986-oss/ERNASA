"""Regression checks: CONTROL first-round questions are the candidate source of truth."""
import json
import sqlite3
import unittest

from control.interview_templates import PARTNER_FIRST_ROUND_NAME, resolve_partner_first_round
from partner_interview import first_round_questions


class PartnerFirstRoundTemplateTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE ik_interview_templates (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, language TEXT NOT NULL,
            questions TEXT NOT NULL)""")
        self.questions = [{"text": f"Kayıtlı soru {i}", "weight": 10} for i in range(1, 11)]

    def tearDown(self):
        self.db.close()

    def add(self, name, questions=None, language="tr"):
        self.db.execute("INSERT INTO ik_interview_templates(name, language, questions) VALUES (?,?,?)",
                        (name, language, json.dumps(self.questions if questions is None else questions)))

    def test_saved_template_reaches_candidate_in_order(self):
        self.add("ERNASA B2B Satış Ortağı")
        self.add("ERNASA B2B Satış Ortağı - 1. Tur")
        template = resolve_partner_first_round(self.db)
        self.assertEqual(template["name"], "ERNASA B2B Satış Ortağı - 1. Tur")
        self.assertEqual(first_round_questions(template),
                         [f"Kayıtlı soru {i}" for i in range(1, 11)])

    def test_canonical_name_preferred_when_both_exist(self):
        self.add("ERNASA B2B Satış Ortağı - 1. Tur")
        self.add(PARTNER_FIRST_ROUND_NAME)
        self.assertEqual(resolve_partner_first_round(self.db)["name"], PARTNER_FIRST_ROUND_NAME)

    def test_legacy_template_is_never_a_fallback(self):
        self.add("ERNASA B2B Satış Ortağı")
        self.assertEqual(resolve_partner_first_round(self.db), {})
        self.assertIsNone(first_round_questions({}))

    def test_invalid_template_fails_closed(self):
        self.add(PARTNER_FIRST_ROUND_NAME, self.questions[:-1])
        self.assertIsNone(first_round_questions(resolve_partner_first_round(self.db)))
        self.db.execute("DELETE FROM ik_interview_templates")
        self.add(PARTNER_FIRST_ROUND_NAME, language="en")
        self.assertIsNone(first_round_questions(resolve_partner_first_round(self.db)))
        self.db.execute("DELETE FROM ik_interview_templates")
        self.add(PARTNER_FIRST_ROUND_NAME, [{**q, "weight": 11} for q in self.questions])
        self.assertIsNone(first_round_questions(resolve_partner_first_round(self.db)))

    def test_broken_json_fails_closed(self):
        self.db.execute("INSERT INTO ik_interview_templates(name, language, questions) VALUES (?,?,?)",
                        (PARTNER_FIRST_ROUND_NAME, "tr", "{broken"))
        self.assertEqual(resolve_partner_first_round(self.db), {})


if __name__ == "__main__":
    unittest.main()
