-- ERNASA Eğitim: PostgreSQL 18, independent tenant-scoped LMS schema.
-- DRAFT ONLY. Not connected to backend startup, not deployed, no existing data touched.
-- Apply to a dedicated education database after authentication and migration review.
BEGIN;

CREATE TABLE IF NOT EXISTS edu_tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
    external_company_id BIGINT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS edu_members (
    tenant_id UUID NOT NULL REFERENCES edu_tenants(id),
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    external_subject TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner','admin','instructor','learner','auditor')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,external_subject)
);

CREATE TABLE IF NOT EXISTS edu_courses (
    tenant_id UUID NOT NULL REFERENCES edu_tenants(id),
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','archived')),
    created_by UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,code),
    FOREIGN KEY (tenant_id,created_by) REFERENCES edu_members(tenant_id,id)
);

CREATE TABLE IF NOT EXISTS edu_course_versions (
    tenant_id UUID NOT NULL,
    course_id UUID NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','retired')),
    published_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id,course_id,version),
    FOREIGN KEY (tenant_id,course_id) REFERENCES edu_courses(tenant_id,id),
    CHECK (status <> 'published' OR published_at IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS edu_lessons (
    tenant_id UUID NOT NULL,
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    course_id UUID NOT NULL,
    course_version INTEGER NOT NULL,
    lesson_order INTEGER NOT NULL CHECK (lesson_order > 0),
    title TEXT NOT NULL,
    content_type TEXT NOT NULL CHECK (content_type IN ('text','video','document','interactive')),
    content_ref TEXT NOT NULL,
    required BOOLEAN NOT NULL DEFAULT TRUE,
    estimated_minutes INTEGER NOT NULL DEFAULT 0 CHECK (estimated_minutes >= 0),
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,course_id,course_version,lesson_order),
    UNIQUE (tenant_id,course_id,course_version,id),
    FOREIGN KEY (tenant_id,course_id,course_version)
        REFERENCES edu_course_versions(tenant_id,course_id,version)
);

CREATE TABLE IF NOT EXISTS edu_enrollments (
    tenant_id UUID NOT NULL,
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    member_id UUID NOT NULL,
    course_id UUID NOT NULL,
    course_version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'assigned'
        CHECK (status IN ('assigned','in_progress','completed','cancelled')),
    deadline TIMESTAMPTZ,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,id,course_id,course_version),
    UNIQUE (tenant_id,member_id,course_id,course_version),
    FOREIGN KEY (tenant_id,member_id) REFERENCES edu_members(tenant_id,id),
    FOREIGN KEY (tenant_id,course_id,course_version)
        REFERENCES edu_course_versions(tenant_id,course_id,version)
);

CREATE TABLE IF NOT EXISTS edu_lesson_progress (
    tenant_id UUID NOT NULL,
    enrollment_id UUID NOT NULL,
    course_id UUID NOT NULL,
    course_version INTEGER NOT NULL,
    lesson_id UUID NOT NULL,
    percent SMALLINT NOT NULL DEFAULT 0 CHECK (percent BETWEEN 0 AND 100),
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,enrollment_id,lesson_id),
    FOREIGN KEY (tenant_id,enrollment_id,course_id,course_version)
        REFERENCES edu_enrollments(tenant_id,id,course_id,course_version),
    FOREIGN KEY (tenant_id,course_id,course_version,lesson_id)
        REFERENCES edu_lessons(tenant_id,course_id,course_version,id),
    CHECK (completed_at IS NULL OR percent = 100)
);

CREATE TABLE IF NOT EXISTS edu_assessments (
    tenant_id UUID NOT NULL,
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    course_id UUID NOT NULL,
    course_version INTEGER NOT NULL,
    title TEXT NOT NULL,
    passing_percent NUMERIC(5,2) NOT NULL CHECK (passing_percent BETWEEN 0 AND 100),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    time_limit_minutes INTEGER CHECK (time_limit_minutes IS NULL OR time_limit_minutes > 0),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','retired')),
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,id,course_id,course_version),
    FOREIGN KEY (tenant_id,course_id,course_version)
        REFERENCES edu_course_versions(tenant_id,course_id,version)
);

CREATE TABLE IF NOT EXISTS edu_questions (
    tenant_id UUID NOT NULL,
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    assessment_id UUID NOT NULL,
    question_order INTEGER NOT NULL CHECK (question_order > 0),
    kind TEXT NOT NULL CHECK (kind IN ('single_choice','multiple_choice','manual_review')),
    prompt TEXT NOT NULL,
    options JSONB NOT NULL DEFAULT '[]'::jsonb,
    answer_key JSONB, -- Server-only: never return this column in learner APIs.
    points INTEGER NOT NULL CHECK (points > 0),
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,assessment_id,question_order),
    FOREIGN KEY (tenant_id,assessment_id) REFERENCES edu_assessments(tenant_id,id)
);

CREATE TABLE IF NOT EXISTS edu_attempts (
    tenant_id UUID NOT NULL,
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    enrollment_id UUID NOT NULL,
    assessment_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    state TEXT NOT NULL DEFAULT 'started'
        CHECK (state IN ('started','submitted','pending_review','graded','void')),
    answers JSONB NOT NULL DEFAULT '{}'::jsonb,
    score_percent NUMERIC(5,2) CHECK (score_percent BETWEEN 0 AND 100),
    passed BOOLEAN,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitted_at TIMESTAMPTZ,
    reviewed_by UUID,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,enrollment_id,assessment_id,attempt_number),
    FOREIGN KEY (tenant_id,enrollment_id) REFERENCES edu_enrollments(tenant_id,id),
    FOREIGN KEY (tenant_id,assessment_id) REFERENCES edu_assessments(tenant_id,id),
    FOREIGN KEY (tenant_id,reviewed_by) REFERENCES edu_members(tenant_id,id),
    CHECK (state NOT IN ('graded','pending_review') OR submitted_at IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS edu_learning_plans (
    tenant_id UUID NOT NULL,
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    member_id UUID NOT NULL,
    course_id UUID,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed','approved','active','completed','cancelled')),
    source TEXT NOT NULL CHECK (source IN ('instructor','rules','ai_recommendation')),
    plan JSONB NOT NULL,
    approved_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,id),
    FOREIGN KEY (tenant_id,member_id) REFERENCES edu_members(tenant_id,id),
    FOREIGN KEY (tenant_id,course_id) REFERENCES edu_courses(tenant_id,id),
    FOREIGN KEY (tenant_id,approved_by) REFERENCES edu_members(tenant_id,id),
    CHECK (status NOT IN ('approved','active','completed') OR approved_by IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS edu_certificates (
    tenant_id UUID NOT NULL,
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    enrollment_id UUID NOT NULL,
    certificate_number TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id,id),
    UNIQUE (tenant_id,certificate_number),
    FOREIGN KEY (tenant_id,enrollment_id) REFERENCES edu_enrollments(tenant_id,id)
);

CREATE TABLE IF NOT EXISTS edu_entitlements (
    tenant_id UUID PRIMARY KEY REFERENCES edu_tenants(id),
    plan_code TEXT NOT NULL CHECK (plan_code IN ('starter','growth','business','pro')),
    active_from TIMESTAMPTZ NOT NULL,
    active_until TIMESTAMPTZ,
    learner_limit INTEGER CHECK (learner_limit IS NULL OR learner_limit >= 0),
    feature_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS edu_audit_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES edu_tenants(id),
    actor_member_id UUID,
    action TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id,actor_member_id) REFERENCES edu_members(tenant_id,id)
);

CREATE INDEX IF NOT EXISTS edu_enrollments_member_status_idx
    ON edu_enrollments(tenant_id,member_id,status);
CREATE INDEX IF NOT EXISTS edu_lesson_progress_enrollment_idx
    ON edu_lesson_progress(tenant_id,enrollment_id);
CREATE INDEX IF NOT EXISTS edu_attempts_enrollment_idx
    ON edu_attempts(tenant_id,enrollment_id,started_at);
CREATE INDEX IF NOT EXISTS edu_audit_tenant_time_idx
    ON edu_audit_events(tenant_id,occurred_at DESC);

COMMIT;
