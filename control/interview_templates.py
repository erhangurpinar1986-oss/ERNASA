"""CONTROL-owned interview templates; existing interview questions remain immutable."""
import json

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
