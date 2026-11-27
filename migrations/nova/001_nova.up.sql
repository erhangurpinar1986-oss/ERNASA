-- NOVA 2.0 AI SALES CENTER. Additive, shared SQLite/PostgreSQL DDL; apply inside one transaction.
-- Only new nova_* tables. Existing CONTROL, Sales Hub, HR and Academy tables are never modified.
-- Separate migration family (migrations/nova) so CONTROL/SECURITY numbering (control_postgresql/NNN)
-- is never contested.

-- Single settings row. Every outbound channel starts CLOSED; only CONTROL founder may open it.
CREATE TABLE IF NOT EXISTS nova_settings(
 id INTEGER PRIMARY KEY CHECK(id=1),
 running INTEGER NOT NULL DEFAULT 0 CHECK(running IN (0,1)),
 kill_switch INTEGER NOT NULL DEFAULT 1 CHECK(kill_switch IN (0,1)),
 auto_email INTEGER NOT NULL DEFAULT 0 CHECK(auto_email IN (0,1)),
 auto_call INTEGER NOT NULL DEFAULT 0 CHECK(auto_call IN (0,1)),
 email_policy_approved INTEGER NOT NULL DEFAULT 0 CHECK(email_policy_approved IN (0,1)),
 call_policy_approved INTEGER NOT NULL DEFAULT 0 CHECK(call_policy_approved IN (0,1)),
 require_iys INTEGER NOT NULL DEFAULT 1 CHECK(require_iys IN (0,1)),
 daily_research_target INTEGER NOT NULL DEFAULT 20 CHECK(daily_research_target BETWEEN 0 AND 500),
 daily_email_limit INTEGER NOT NULL DEFAULT 10 CHECK(daily_email_limit BETWEEN 0 AND 200),
 daily_call_limit INTEGER NOT NULL DEFAULT 5 CHECK(daily_call_limit BETWEEN 0 AND 200),
 max_call_seconds INTEGER NOT NULL DEFAULT 300 CHECK(max_call_seconds BETWEEN 30 AND 1800),
 daily_spend_limit_minor BIGINT NOT NULL DEFAULT 20000 CHECK(daily_spend_limit_minor>=0),
 monthly_spend_limit_minor BIGINT NOT NULL DEFAULT 300000 CHECK(monthly_spend_limit_minor>=0),
 call_rate_minor_per_minute BIGINT NOT NULL DEFAULT 1000 CHECK(call_rate_minor_per_minute>=0),
 work_start TEXT NOT NULL DEFAULT '09:30',
 work_end TEXT NOT NULL DEFAULT '17:30',
 work_days TEXT NOT NULL DEFAULT '1,2,3,4,5',
 target_sectors TEXT NOT NULL DEFAULT '',
 target_regions TEXT NOT NULL DEFAULT '',
 product_priority TEXT NOT NULL DEFAULT 'hr_flow',
 handoff_rules TEXT NOT NULL DEFAULT 'Fiyat indirimi, sözleşme, özel ödeme koşulu, hukuki soru, şikâyet veya karşı taraf isterse insan temsilciye aktar.',
 updated_by TEXT NOT NULL DEFAULT 'migration',
 updated_at TEXT NOT NULL DEFAULT '');
INSERT INTO nova_settings(id) VALUES(1) ON CONFLICT(id) DO NOTHING;

-- ERNASA DATA POOL. Only corporate, publicly published business data with its source URL.
CREATE TABLE IF NOT EXISTS nova_companies(
 id TEXT PRIMARY KEY,
 legal_name TEXT NOT NULL,
 name_key TEXT NOT NULL,
 domain TEXT,
 tax_id TEXT,
 sector TEXT NOT NULL DEFAULT '',
 city TEXT NOT NULL DEFAULT '',
 district TEXT NOT NULL DEFAULT '',
 website TEXT NOT NULL DEFAULT '',
 phone TEXT NOT NULL DEFAULT '',
 phone_source_url TEXT NOT NULL DEFAULT '',
 email TEXT NOT NULL DEFAULT '',
 email_source_url TEXT NOT NULL DEFAULT '',
 email_invalid INTEGER NOT NULL DEFAULT 0 CHECK(email_invalid IN (0,1)),
 contact_department TEXT NOT NULL DEFAULT '',
 contact_title TEXT NOT NULL DEFAULT '',
 linkedin_url TEXT NOT NULL DEFAULT '',
 employee_count TEXT NOT NULL DEFAULT '',
 employee_source_url TEXT NOT NULL DEFAULT '',
 open_jobs TEXT NOT NULL DEFAULT '',
 job_source_url TEXT NOT NULL DEFAULT '',
 job_seen_on TEXT NOT NULL DEFAULT '',
 hiring_need TEXT NOT NULL DEFAULT '',
 product_fit TEXT NOT NULL DEFAULT '',
 suggested_package TEXT NOT NULL DEFAULT '',
 score INTEGER NOT NULL DEFAULT 0 CHECK(score BETWEEN 0 AND 100),
 score_detail TEXT NOT NULL DEFAULT '{}',
 source_url TEXT NOT NULL DEFAULT '',
 verified_on TEXT NOT NULL DEFAULT '',
 consent_status TEXT NOT NULL DEFAULT 'b2b_unverified' CHECK(consent_status IN ('b2b_unverified','b2b_corporate','opted_in','objected')),
 call_permission TEXT NOT NULL DEFAULT '',
 iys_checked_on TEXT NOT NULL DEFAULT '',
 stage TEXT NOT NULL DEFAULT 'new' CHECK(stage IN ('new','researching','verified','contactable','email_draft','email_sent',
   'awaiting_reply','call_planned','call_done','interested','demo_requested','handed_off','proposal','won','rejected','do_not_contact')),
 owner_account_id TEXT,
 sales_customer_id TEXT,
 lock_holder TEXT NOT NULL DEFAULT '',
 lock_until BIGINT NOT NULL DEFAULT 0,
 synthetic INTEGER NOT NULL DEFAULT 0 CHECK(synthetic IN (0,1)),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS nova_companies_domain ON nova_companies(domain) WHERE domain IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS nova_companies_tax ON nova_companies(tax_id) WHERE tax_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS nova_companies_name_city ON nova_companies(name_key,city);
CREATE INDEX IF NOT EXISTS nova_companies_stage ON nova_companies(stage);

-- Append-only, timestamped history for every stage change, assignment, message and call.
CREATE TABLE IF NOT EXISTS nova_events(
 id TEXT PRIMARY KEY,
 company_id TEXT REFERENCES nova_companies(id),
 kind TEXT NOT NULL,
 from_stage TEXT NOT NULL DEFAULT '',
 to_stage TEXT NOT NULL DEFAULT '',
 detail TEXT NOT NULL DEFAULT '',
 actor TEXT NOT NULL,
 stamp TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS nova_events_company ON nova_events(company_id,stamp);

-- Objection / do-not-contact list. Applies to every channel. Never deleted by the application.
CREATE TABLE IF NOT EXISTS nova_suppressions(
 id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('email','domain','phone','company')),
 value TEXT NOT NULL,
 reason TEXT NOT NULL,
 source TEXT NOT NULL,
 stamp TEXT NOT NULL,
 UNIQUE(kind,value));

CREATE TABLE IF NOT EXISTS nova_emails(
 id TEXT PRIMARY KEY,
 company_id TEXT NOT NULL REFERENCES nova_companies(id),
 direction TEXT NOT NULL CHECK(direction IN ('out','in')),
 address TEXT NOT NULL,
 subject TEXT NOT NULL,
 body TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('draft','approved','sent','delivered','bounced','failed','cancelled','received')),
 classification TEXT NOT NULL DEFAULT '',
 campaign TEXT NOT NULL DEFAULT 'intro',
 dedupe_key TEXT UNIQUE,
 provider TEXT NOT NULL DEFAULT '',
 provider_id TEXT NOT NULL DEFAULT '',
 approved_by TEXT NOT NULL DEFAULT '',
 error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 sent_at TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS nova_emails_status ON nova_emails(status,sent_at);
CREATE UNIQUE INDEX IF NOT EXISTS nova_emails_inbound ON nova_emails(provider_id) WHERE direction='in' AND provider_id<>'';

-- Founder-authorized test numbers. Only these can be dialled before the call policy is approved.
CREATE TABLE IF NOT EXISTS nova_test_numbers(
 phone TEXT PRIMARY KEY,
 label TEXT NOT NULL,
 authorized_by TEXT NOT NULL,
 stamp TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS nova_calls(
 id TEXT PRIMARY KEY,
 company_id TEXT REFERENCES nova_companies(id),
 phone TEXT NOT NULL,
 purpose TEXT NOT NULL CHECK(purpose IN ('founder_test','prospect')),
 status TEXT NOT NULL CHECK(status IN ('planned','dialing','completed','failed','no_answer','declined_ai','cancelled')),
 provider TEXT NOT NULL DEFAULT '',
 provider_call_id TEXT UNIQUE,
 department TEXT NOT NULL DEFAULT '',
 started_at TEXT NOT NULL DEFAULT '',
 ended_at TEXT NOT NULL DEFAULT '',
 duration_seconds INTEGER NOT NULL DEFAULT 0 CHECK(duration_seconds>=0),
 cost_minor BIGINT NOT NULL DEFAULT 0 CHECK(cost_minor>=0),
 cost_source TEXT NOT NULL DEFAULT '',
 outcome TEXT NOT NULL DEFAULT '',
 needs TEXT NOT NULL DEFAULT '',
 questions TEXT NOT NULL DEFAULT '',
 objections TEXT NOT NULL DEFAULT '',
 interest TEXT NOT NULL DEFAULT '',
 demo_requested INTEGER NOT NULL DEFAULT 0 CHECK(demo_requested IN (0,1)),
 next_step TEXT NOT NULL DEFAULT '',
 summary TEXT NOT NULL DEFAULT '',
 recording_allowed INTEGER NOT NULL DEFAULT 0 CHECK(recording_allowed IN (0,1)),
 transcript TEXT,
 retention_until TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS nova_calls_created ON nova_calls(created_at);

CREATE TABLE IF NOT EXISTS nova_demo_requests(
 id TEXT PRIMARY KEY,
 company_id TEXT NOT NULL REFERENCES nova_companies(id),
 source TEXT NOT NULL CHECK(source IN ('email','call','manual')),
 note TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','assigned','scheduled','done','cancelled')),
 created_at TEXT NOT NULL);

-- Central product knowledge. NOVA may only state 'approved' entries to customers.
CREATE TABLE IF NOT EXISTS nova_knowledge(
 id TEXT PRIMARY KEY,
 product TEXT NOT NULL,
 topic TEXT NOT NULL,
 question TEXT NOT NULL,
 answer TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('approved','draft','not_live')),
 source TEXT NOT NULL,
 approved_by TEXT NOT NULL DEFAULT '',
 updated_at TEXT NOT NULL,
 UNIQUE(product,topic,question));

-- Real, provider-reported or estimated spend (kuruş). Limits are checked against this ledger.
CREATE TABLE IF NOT EXISTS nova_spend(
 id TEXT PRIMARY KEY,
 day TEXT NOT NULL,
 category TEXT NOT NULL CHECK(category IN ('call','email','llm','research','reservation')),
 amount_minor BIGINT NOT NULL,
 reference TEXT NOT NULL,
 stamp TEXT NOT NULL,
 UNIQUE(category,reference));
CREATE INDEX IF NOT EXISTS nova_spend_day ON nova_spend(day);

-- Durable work queue: retries and state survive restarts; no dependency on any local computer.
CREATE TABLE IF NOT EXISTS nova_jobs(
 id TEXT PRIMARY KEY,
 kind TEXT NOT NULL,
 payload TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','done','failed','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 3,
 run_after BIGINT NOT NULL DEFAULT 0,
 last_error TEXT NOT NULL DEFAULT '',
 dedupe_key TEXT UNIQUE,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS nova_jobs_ready ON nova_jobs(status,run_after);

CREATE TABLE IF NOT EXISTS nova_reports(
 day TEXT PRIMARY KEY,
 body TEXT NOT NULL,
 created_at TEXT NOT NULL);
