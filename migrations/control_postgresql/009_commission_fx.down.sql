-- Rollback for 009 only. Export first if it holds real entitlements.
DROP TRIGGER IF EXISTS sales_commission_fx_immutable ON ernasa_control.sales_commission_fx;
DROP TABLE IF EXISTS ernasa_control.sales_commission_fx;
