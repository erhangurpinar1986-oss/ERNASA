-- PostgreSQL only. Apply after 001_nova.up.sql in the SAME transaction with
-- SET LOCAL search_path = ernasa_control, pg_catalog. Idempotent (CREATE OR REPLACE).
-- Own function name: sales_reject_history_mutation (Sales Hub) is reused by nobody here.
CREATE OR REPLACE FUNCTION ernasa_control.nova_reject_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'NOVA history and objection list are append-only';
END;
$$;
CREATE OR REPLACE TRIGGER nova_events_immutable BEFORE UPDATE OR DELETE ON ernasa_control.nova_events
 FOR EACH ROW EXECUTE FUNCTION ernasa_control.nova_reject_history_mutation();
CREATE OR REPLACE TRIGGER nova_events_no_truncate BEFORE TRUNCATE ON ernasa_control.nova_events
 FOR EACH STATEMENT EXECUTE FUNCTION ernasa_control.nova_reject_history_mutation();
CREATE OR REPLACE TRIGGER nova_suppressions_immutable BEFORE UPDATE OR DELETE ON ernasa_control.nova_suppressions
 FOR EACH ROW EXECUTE FUNCTION ernasa_control.nova_reject_history_mutation();
CREATE OR REPLACE TRIGGER nova_suppressions_no_truncate BEFORE TRUNCATE ON ernasa_control.nova_suppressions
 FOR EACH STATEMENT EXECUTE FUNCTION ernasa_control.nova_reject_history_mutation();
