-- SQLite only. Additive guards; an application rollback must keep this history.
CREATE TRIGGER IF NOT EXISTS sales_audit_no_update BEFORE UPDATE ON sales_audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
CREATE TRIGGER IF NOT EXISTS sales_audit_no_delete BEFORE DELETE ON sales_audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
CREATE TRIGGER IF NOT EXISTS sales_decisions_no_update BEFORE UPDATE ON sales_decisions BEGIN SELECT RAISE(ABORT,'immutable decision'); END;
CREATE TRIGGER IF NOT EXISTS sales_decisions_no_delete BEFORE DELETE ON sales_decisions BEGIN SELECT RAISE(ABORT,'immutable decision'); END;
CREATE TRIGGER IF NOT EXISTS sales_adjustments_no_update BEFORE UPDATE ON sales_adjustments BEGIN SELECT RAISE(ABORT,'immutable adjustment'); END;
CREATE TRIGGER IF NOT EXISTS sales_adjustments_no_delete BEFORE DELETE ON sales_adjustments BEGIN SELECT RAISE(ABORT,'immutable adjustment'); END;
