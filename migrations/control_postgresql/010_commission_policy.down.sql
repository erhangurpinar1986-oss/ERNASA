-- Rollback for 010. Restoring the narrower (0,5,25) checks fails while %2 rows exist; such rows
-- are real entitlements and must be reviewed first. The upgrade detail table is dropped last.
DROP TRIGGER IF EXISTS sales_commission_upgrades_immutable ON ernasa_control.sales_commission_upgrades;
DROP TABLE IF EXISTS ernasa_control.sales_commission_upgrades;
ALTER TABLE ernasa_control.sales_commission_fx DROP CONSTRAINT IF EXISTS sales_commission_fx_rate_policy_2026;
ALTER TABLE ernasa_control.sales_commission_fx ADD CONSTRAINT sales_commission_fx_rate_check CHECK (rate IN (0, 5, 25));
ALTER TABLE ernasa_control.sales_commissions DROP CONSTRAINT IF EXISTS sales_commissions_rate_policy_2026;
ALTER TABLE ernasa_control.sales_commissions ADD CONSTRAINT sales_commissions_rate_check CHECK (rate IN (0, 5, 25));
