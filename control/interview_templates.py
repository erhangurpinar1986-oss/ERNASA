"""CONTROL-owned interview templates; existing interview questions remain immutable."""
import json
from pathlib import Path

PARTNER_INTRODUCTION = 'Hoş geldiniz. Kendinizi ve satış veya iş geliştirme deneyiminizi kısaca tanıtır mısınız?'
PARTNER_INTRODUCTION_LIMIT = 700
PARTNER_ENGLISH_DEMO_NAME = 'ERNASA International Sales · Round 2'
PARTNER_ENGLISH_DEMO = (
    'Imagine I’m an HR manager speaking with you for the first time. In 60 seconds, explain how ERNASA '
    'could help my hiring team. Then answer this question: “Does it integrate with our current ATS, '
    'and is that included in the price?”'
)


PARTNER_FIRST_ROUND_NAME = 'ERNASA B2B Satış Ortağı · 1. Tur'
PARTNER_FIRST_ROUND_WEIGHTS = (13, 20, 10, 10, 5, 8, 10, 12, 5, 7)
PARTNER_FIRST_ROUND_ALIASES = (PARTNER_FIRST_ROUND_NAME, 'ERNASA B2B Satış Ortağı - 1. Tur')


def resolve_partner_first_round(db):
    """Read the saved CONTROL first-round template; never substitute the legacy template."""
    row = db.execute("""SELECT name,language,questions FROM ik_interview_templates
        WHERE name IN (?,?) ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END LIMIT 1""",
        (*PARTNER_FIRST_ROUND_ALIASES, PARTNER_FIRST_ROUND_NAME)).fetchone()
    if not row:
        return {}
    try:
        questions = json.loads(row['questions'])
    except (TypeError, ValueError):
        return {}
    return {'name': row['name'], 'language': row['language'], 'questions': questions}


def partner_first_round():
    path = Path(__file__).resolve().parents[1] / 'ernasa-locked-questions.json'
    questions = json.loads(path.read_text(encoding='utf-8-sig'))
    return [{'text':text,'weight':weight}
            for text,weight in zip(questions,PARTNER_FIRST_ROUND_WEIGHTS,strict=True)]


def snapshot_question_count(snapshot):
    return len(json.loads(snapshot['questions'])) if snapshot else 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS ik_interview_templates (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, language TEXT NOT NULL,
    questions TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ik_interview_template_assignments (
    company_id INTEGER NOT NULL REFERENCES companies(id), position_id TEXT NOT NULL DEFAULT '',
    template_id INTEGER NOT NULL REFERENCES ik_interview_templates(id),
    PRIMARY KEY(company_id,position_id));
"""


def resolve(db, company_id, position_id):
    row = db.execute("""SELECT t.name,t.language,t.questions
        FROM ik_interview_template_assignments a JOIN ik_interview_templates t ON t.id=a.template_id
        WHERE a.company_id=? AND a.position_id IN ('',?) ORDER BY a.position_id DESC LIMIT 1""",
        (company_id, position_id)).fetchone()
    if not row:
        return {}
    return {'name':row['name'],'language':row['language'],'questions':json.loads(row['questions'])}
