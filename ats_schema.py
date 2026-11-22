"""Additive storage for ATS records. Existing AI interviews remain independent unless linked."""
DDL = (
    """CREATE TABLE IF NOT EXISTS ats_departments (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL,
        UNIQUE(company_id,name_key))""",
    """CREATE TABLE IF NOT EXISTS ats_candidates (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, name TEXT NOT NULL,
        email TEXT, phone TEXT NOT NULL, source TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
        created_by BIGINT, created_at TEXT NOT NULL, UNIQUE(company_id,email))""",
    """CREATE TABLE IF NOT EXISTS ats_jobs (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, position_id TEXT NOT NULL REFERENCES company_positions,
        title TEXT NOT NULL, summary TEXT NOT NULL, state TEXT NOT NULL,
        notice TEXT NOT NULL, notice_version TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(company_id,position_id))""",
    """CREATE TABLE IF NOT EXISTS ats_job_details (
        job_id TEXT PRIMARY KEY REFERENCES ats_jobs, company_id BIGINT NOT NULL,
        qualifications TEXT NOT NULL, work_conditions TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ats_job_deadlines (
        job_id TEXT PRIMARY KEY REFERENCES ats_jobs(id) ON DELETE CASCADE,
        company_id BIGINT NOT NULL, closes_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ats_candidate_email_conflicts (
        candidate_id TEXT PRIMARY KEY REFERENCES ats_candidates(id) ON DELETE CASCADE,
        company_id BIGINT NOT NULL, reported_email TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS ats_candidate_email_conflicts_lookup
        ON ats_candidate_email_conflicts(company_id,reported_email)""",
    """CREATE TABLE IF NOT EXISTS ats_applications (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, candidate_id TEXT NOT NULL REFERENCES ats_candidates,
        position_id TEXT NOT NULL REFERENCES company_positions, job_id TEXT REFERENCES ats_jobs,
        stage TEXT NOT NULL, closure TEXT, created_by BIGINT, assigned_user_id BIGINT,
        revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, changed_at TEXT NOT NULL,
        UNIQUE(company_id,candidate_id,position_id))""",
    """CREATE TABLE IF NOT EXISTS ats_position_profiles (
        position_id TEXT PRIMARY KEY REFERENCES company_positions, company_id BIGINT NOT NULL,
        code TEXT NOT NULL, state TEXT NOT NULL, fields TEXT NOT NULL, UNIQUE(company_id,code))""",
    """CREATE TABLE IF NOT EXISTS ats_position_members (
        company_id BIGINT NOT NULL, position_id TEXT NOT NULL REFERENCES company_positions,
        user_id BIGINT NOT NULL, PRIMARY KEY(company_id,position_id,user_id))""",
    """CREATE TABLE IF NOT EXISTS ats_stage_events (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, application_id TEXT NOT NULL REFERENCES ats_applications,
        user_id BIGINT NOT NULL, previous TEXT NOT NULL, next TEXT NOT NULL, closure TEXT,
        occurred_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ats_notes (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, application_id TEXT NOT NULL REFERENCES ats_applications,
        user_id BIGINT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ik_interview_template_snapshots (
        token TEXT PRIMARY KEY REFERENCES interview_links, company_id BIGINT NOT NULL,
        name TEXT NOT NULL, language TEXT NOT NULL, questions TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ik_ernasa_invites (
        token TEXT PRIMARY KEY REFERENCES interview_links, company_id BIGINT NOT NULL,
        created_by BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ik_partner_drafts (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, created_by BIGINT NOT NULL,
        name TEXT NOT NULL, email TEXT NOT NULL, suggestions TEXT NOT NULL, expires_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ik_partner_interviews (
        token TEXT PRIMARY KEY REFERENCES ik_ernasa_invites, company_id BIGINT NOT NULL,
        question_count INTEGER NOT NULL CHECK(question_count BETWEEN 12 AND 13),
        report TEXT, error TEXT)""",
    """CREATE TABLE IF NOT EXISTS ik_partner_introductions (
        token TEXT PRIMARY KEY REFERENCES ik_partner_interviews ON DELETE CASCADE,
        company_id BIGINT NOT NULL, question TEXT NOT NULL, answer TEXT)""",
    """CREATE TABLE IF NOT EXISTS ik_partner_extra_questions (
        token TEXT NOT NULL REFERENCES ik_partner_interviews,
        question_number INTEGER NOT NULL CHECK(question_number BETWEEN 11 AND 13),
        question TEXT NOT NULL, answer TEXT, answered_at TEXT, PRIMARY KEY(token,question_number))""",
    """CREATE TABLE IF NOT EXISTS ik_partner_cv_evidence (
        token TEXT PRIMARY KEY REFERENCES ik_partner_interviews, company_id BIGINT NOT NULL,
        criteria TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ats_documents (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, application_id TEXT NOT NULL REFERENCES ats_applications,
        filename TEXT NOT NULL, media_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        uploader_id BIGINT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ats_document_digests (
        company_id BIGINT NOT NULL, application_id TEXT NOT NULL REFERENCES ats_applications,
        digest TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES ats_documents,
        PRIMARY KEY(company_id,application_id,digest))""",
    """CREATE TABLE IF NOT EXISTS ats_document_blobs (
        document_id TEXT PRIMARY KEY REFERENCES ats_documents(id) ON DELETE CASCADE,
        company_id BIGINT NOT NULL, application_id TEXT NOT NULL REFERENCES ats_applications,
        digest TEXT NOT NULL, content_base64 TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS flow_screening_results (
            company_id BIGINT NOT NULL, application_id TEXT NOT NULL,
            position_id TEXT NOT NULL, document_id TEXT NOT NULL,
            cv_digest TEXT NOT NULL, criteria_digest TEXT NOT NULL,
            analysis_state TEXT NOT NULL, match_score REAL, coverage REAL,
            details_json TEXT NOT NULL, analyzed_at TEXT NOT NULL,
            PRIMARY KEY(company_id,application_id,document_id)
        )""",
    """CREATE TABLE IF NOT EXISTS flow_screening_jobs (
        document_id TEXT PRIMARY KEY REFERENCES ats_documents(id) ON DELETE CASCADE,
        company_id BIGINT NOT NULL, application_id TEXT NOT NULL,
        available_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS flow_screening_jobs_ready
        ON flow_screening_jobs(available_at,document_id)""",
    """CREATE TABLE IF NOT EXISTS flow_booking_jobs (
        application_id TEXT PRIMARY KEY REFERENCES ats_applications(id) ON DELETE CASCADE,
        company_id BIGINT NOT NULL, document_id TEXT NOT NULL,
        manager_id BIGINT NOT NULL, access_version TEXT NOT NULL,
        slug TEXT NOT NULL, origin TEXT NOT NULL, status TEXT NOT NULL,
        token_hash TEXT UNIQUE, expires_at TEXT, schedule_id TEXT,
        error_code TEXT, updated_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS flow_booking_jobs_ready
        ON flow_booking_jobs(status,updated_at)""",
    """CREATE TABLE IF NOT EXISTS flow_scheduled_interviews (
        token TEXT PRIMARY KEY REFERENCES interview_links(token) ON DELETE CASCADE,
        company_id BIGINT NOT NULL, schedule_id TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ats_interview_cv_sources (
        token TEXT PRIMARY KEY REFERENCES interview_links, company_id BIGINT NOT NULL,
        document_id TEXT NOT NULL REFERENCES ats_documents ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS ats_cv_imports (
        company_id BIGINT NOT NULL, user_id BIGINT NOT NULL, import_id TEXT NOT NULL,
        document_id TEXT NOT NULL REFERENCES ats_documents ON DELETE CASCADE,
        PRIMARY KEY(company_id,user_id,import_id))""",
    """CREATE TABLE IF NOT EXISTS ik_cv_invites (
        company_id BIGINT NOT NULL, user_id BIGINT NOT NULL, request_id TEXT NOT NULL,
        token TEXT NOT NULL REFERENCES interview_links ON DELETE CASCADE, candidate_number TEXT NOT NULL,
        PRIMARY KEY(company_id,user_id,request_id))""",
    """CREATE TABLE IF NOT EXISTS ik_interview_assessments (
        token TEXT PRIMARY KEY REFERENCES interview_links ON DELETE CASCADE,
        company_id BIGINT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL,
        claim_id TEXT, lease_until TEXT)""",
    "CREATE INDEX IF NOT EXISTS ik_interview_assessments_queue ON ik_interview_assessments(status,updated_at)",
    """CREATE TABLE IF NOT EXISTS ats_upload_jobs (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, application_id TEXT NOT NULL REFERENCES ats_applications,
        user_id BIGINT NOT NULL, slug TEXT NOT NULL, access_version TEXT NOT NULL, filename TEXT NOT NULL,
        content_hash TEXT NOT NULL, payload TEXT, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
        available_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ats_upload_jobs_pending ON ats_upload_jobs(status,available_at)",
    """CREATE TABLE IF NOT EXISTS ats_notices (
        application_id TEXT PRIMARY KEY REFERENCES ats_applications, company_id BIGINT NOT NULL,
        notice TEXT NOT NULL, notice_version TEXT NOT NULL, acknowledged_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ats_schedules (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, application_id TEXT NOT NULL REFERENCES ats_applications,
        interviewer_id BIGINT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, timezone TEXT NOT NULL,
        kind TEXT NOT NULL, status TEXT NOT NULL, notes TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS ats_criteria (
        position_id TEXT PRIMARY KEY REFERENCES company_positions, company_id BIGINT NOT NULL, criteria TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ats_evaluations (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, application_id TEXT NOT NULL REFERENCES ats_applications,
        reviewer_id BIGINT NOT NULL, scores TEXT NOT NULL, note TEXT NOT NULL, completed INTEGER NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(company_id,application_id,reviewer_id))""",
    """CREATE TABLE IF NOT EXISTS ats_review_details (
        evaluation_id TEXT PRIMARY KEY REFERENCES ats_evaluations, company_id BIGINT NOT NULL,
        strengths TEXT NOT NULL, improvements TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', approved_by BIGINT, decided_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS ats_offers (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, application_id TEXT NOT NULL REFERENCES ats_applications,
        amount_minor BIGINT NOT NULL, currency TEXT NOT NULL, starts TEXT NOT NULL, terms TEXT NOT NULL,
        status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ats_offer_details (
        offer_id TEXT PRIMARY KEY REFERENCES ats_offers, company_id BIGINT NOT NULL,
        pay_basis TEXT, benefits TEXT NOT NULL, valid_until TEXT)""",
    """CREATE TABLE IF NOT EXISTS ats_privacy_policy (
        company_id BIGINT PRIMARY KEY, retention_days INTEGER NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ats_privacy_requests (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, application_id TEXT NOT NULL REFERENCES ats_applications,
        requested_by BIGINT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)""",
    # ERNASA HR FLOW · İK Karar Merkezi (yalnızca ekleme). Adayın kendi işlemleri ve İK kararı.
    """CREATE TABLE IF NOT EXISTS flow_candidate_events (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, application_id TEXT,
        subject TEXT NOT NULL,
        event TEXT NOT NULL CHECK(event IN ('availability_link_opened','appointment_selected',
            'interview_link_opened','interview_started','interview_completed')),
        occurred_at TEXT NOT NULL, UNIQUE(company_id,subject,event))""",
    """CREATE INDEX IF NOT EXISTS flow_candidate_events_application
        ON flow_candidate_events(company_id,application_id,occurred_at)""",
    """CREATE TABLE IF NOT EXISTS flow_hr_decisions (
        id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, application_id TEXT NOT NULL,
        previous_decision_id TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('approve','reject','second_interview','assign_training')),
        reason TEXT NOT NULL, decided_by BIGINT NOT NULL, decided_at TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, interview_token_ref TEXT, schedule_id TEXT, training_token_hash TEXT,
        UNIQUE(company_id,application_id,previous_decision_id), UNIQUE(company_id,idempotency_key))""",
    """CREATE INDEX IF NOT EXISTS flow_hr_decisions_application
        ON flow_hr_decisions(company_id,application_id,decided_at)""",
    "CREATE INDEX IF NOT EXISTS ats_applications_company_position ON ats_applications(company_id,position_id)",
    "CREATE INDEX IF NOT EXISTS ats_applications_assignment ON ats_applications(company_id,assigned_user_id,created_by)",
    "CREATE INDEX IF NOT EXISTS ats_schedules_company_date ON ats_schedules(company_id,starts_at)",
    "CREATE INDEX IF NOT EXISTS ats_history_application ON ats_stage_events(company_id,application_id,occurred_at)",
)


def initialize(cursor):
    for statement in DDL:
        cursor.execute(statement)
