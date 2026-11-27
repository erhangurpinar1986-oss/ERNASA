-- MANUAL OPERATOR ROLLBACK ONLY. Never run automatically. Removing these tables deletes the NOVA
-- data pool, contact history and the objection list; export nova_suppressions first and keep it,
-- because objections must keep being honoured. Preferred rollback: redeploy the previous app
-- version and leave these additive tables in place.
SET LOCAL search_path = ernasa_control, pg_catalog;
DROP TRIGGER IF EXISTS nova_events_immutable ON nova_events;
DROP TRIGGER IF EXISTS nova_events_no_truncate ON nova_events;
DROP TRIGGER IF EXISTS nova_suppressions_immutable ON nova_suppressions;
DROP TRIGGER IF EXISTS nova_suppressions_no_truncate ON nova_suppressions;
DROP TABLE IF EXISTS nova_reports, nova_jobs, nova_spend, nova_knowledge, nova_demo_requests,
  nova_calls, nova_test_numbers, nova_emails, nova_suppressions, nova_events, nova_companies, nova_settings;
DROP FUNCTION IF EXISTS nova_reject_history_mutation();
