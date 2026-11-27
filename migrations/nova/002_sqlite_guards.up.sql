-- SQLite only. NOVA history and objection list are append-only.
CREATE TRIGGER IF NOT EXISTS nova_events_no_update BEFORE UPDATE ON nova_events BEGIN SELECT RAISE(ABORT,'immutable nova history'); END;
CREATE TRIGGER IF NOT EXISTS nova_events_no_delete BEFORE DELETE ON nova_events BEGIN SELECT RAISE(ABORT,'immutable nova history'); END;
CREATE TRIGGER IF NOT EXISTS nova_suppressions_no_update BEFORE UPDATE ON nova_suppressions BEGIN SELECT RAISE(ABORT,'immutable objection list'); END;
CREATE TRIGGER IF NOT EXISTS nova_suppressions_no_delete BEFORE DELETE ON nova_suppressions BEGIN SELECT RAISE(ABORT,'immutable objection list'); END;
