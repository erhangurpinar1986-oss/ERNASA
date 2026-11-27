-- Commission policy 2026-11-27: allow the %2 package-upgrade rate and record upgrade details.
-- Idempotent (re-run at every CONTROL start). Existing rows are never changed: the old checks
-- (0,5,25) are replaced by a strictly wider check (0,2,5,25), so every stored row stays valid.
DO $$
DECLARE c record;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sales_commissions_rate_policy_2026'
                   AND conrelid = 'ernasa_control.sales_commissions'::regclass) THEN
        FOR c IN SELECT conname FROM pg_constraint
                 WHERE conrelid = 'ernasa_control.sales_commissions'::regclass AND contype = 'c'
                   AND pg_get_constraintdef(oid) LIKE '%rate%' AND pg_get_constraintdef(oid) LIKE '%25%' LOOP
            EXECUTE format('ALTER TABLE ernasa_control.sales_commissions DROP CONSTRAINT %I', c.conname);
        END LOOP;
        ALTER TABLE ernasa_control.sales_commissions
            ADD CONSTRAINT sales_commissions_rate_policy_2026 CHECK (rate IN (0, 2, 5, 25));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sales_commission_fx_rate_policy_2026'
                   AND conrelid = 'ernasa_control.sales_commission_fx'::regclass) THEN
        FOR c IN SELECT conname FROM pg_constraint
                 WHERE conrelid = 'ernasa_control.sales_commission_fx'::regclass AND contype = 'c'
                   AND pg_get_constraintdef(oid) LIKE '%rate%' AND pg_get_constraintdef(oid) LIKE '%25%' LOOP
            EXECUTE format('ALTER TABLE ernasa_control.sales_commission_fx DROP CONSTRAINT %I', c.conname);
        END LOOP;
        ALTER TABLE ernasa_control.sales_commission_fx
            ADD CONSTRAINT sales_commission_fx_rate_policy_2026 CHECK (rate IN (0, 2, 5, 25));
    END IF;
END $$;

-- Package upgrade month: old/new USD basis, positive difference and the %2 USD commission.
CREATE TABLE IF NOT EXISTS ernasa_control.sales_commission_upgrades (
    commission_id TEXT PRIMARY KEY REFERENCES ernasa_control.sales_commissions(id),
    product TEXT NOT NULL,
    from_plan TEXT NOT NULL,
    to_plan TEXT NOT NULL,
    old_basis_usd NUMERIC(12, 2) NOT NULL CHECK (old_basis_usd >= 0),
    new_basis_usd NUMERIC(12, 2) NOT NULL CHECK (new_basis_usd > old_basis_usd),
    diff_usd NUMERIC(12, 2) NOT NULL CHECK (diff_usd > 0),
    commission_usd NUMERIC(12, 2) NOT NULL CHECK (commission_usd >= 0),
    company_id BIGINT NOT NULL,
    collected_month TEXT NOT NULL,
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS sales_commission_upgrades_month
    ON ernasa_control.sales_commission_upgrades(company_id, product, collected_month);
CREATE OR REPLACE TRIGGER sales_commission_upgrades_immutable
BEFORE UPDATE OR DELETE ON ernasa_control.sales_commission_upgrades
FOR EACH ROW EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();
