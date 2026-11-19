-- Apply after 001/002 and migrations/sales/001_sales.up.sql in the SAME transaction,
-- using SET LOCAL search_path = ernasa_control, pg_catalog before shared DDL.
-- No existing user, HR, academy, company or interview records are modified.
CREATE OR REPLACE FUNCTION ernasa_control.sales_reject_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'Sales history is append-only';
END;
$$;
CREATE OR REPLACE TRIGGER sales_audit_immutable BEFORE UPDATE OR DELETE ON ernasa_control.sales_audit
 FOR EACH ROW EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();
CREATE OR REPLACE TRIGGER sales_decisions_immutable BEFORE UPDATE OR DELETE ON ernasa_control.sales_decisions
 FOR EACH ROW EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();
CREATE OR REPLACE TRIGGER sales_adjustments_immutable BEFORE UPDATE OR DELETE ON ernasa_control.sales_adjustments
 FOR EACH ROW EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();
CREATE OR REPLACE TRIGGER sales_audit_no_truncate BEFORE TRUNCATE ON ernasa_control.sales_audit
 FOR EACH STATEMENT EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();
CREATE OR REPLACE TRIGGER sales_decisions_no_truncate BEFORE TRUNCATE ON ernasa_control.sales_decisions
 FOR EACH STATEMENT EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();
CREATE OR REPLACE TRIGGER sales_adjustments_no_truncate BEFORE TRUNCATE ON ernasa_control.sales_adjustments
 FOR EACH STATEMENT EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();
