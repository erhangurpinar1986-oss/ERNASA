-- Additive: USD-based commission with the TRY entitlement fixed at the sale's TCMB rate.
-- One row per commission created from a verified Shopier / bank collection. Stored separately:
-- USD basis, rate, USD commission, the exchange rate used, its TCMB bulletin date and the fixed TRY
-- amount (equal to sales_commissions.amount_minor). Append-only: never recalculated later.
CREATE TABLE IF NOT EXISTS ernasa_control.sales_commission_fx (
    commission_id TEXT PRIMARY KEY REFERENCES ernasa_control.sales_commissions(id),
    basis_usd NUMERIC(12, 2) NOT NULL CHECK (basis_usd >= 0),
    rate INTEGER NOT NULL CHECK (rate IN (0, 2, 5, 25)),  -- widened in place for new installs; 010 widens existing DBs
    commission_usd NUMERIC(12, 2) NOT NULL CHECK (commission_usd >= 0),
    fx_rate NUMERIC(20, 8) NOT NULL CHECK (fx_rate > 0),
    fx_date DATE NOT NULL,
    fx_source TEXT NOT NULL,
    amount_minor BIGINT NOT NULL CHECK (amount_minor >= 0),
    created_at BIGINT NOT NULL
);
CREATE OR REPLACE TRIGGER sales_commission_fx_immutable
BEFORE UPDATE OR DELETE ON ernasa_control.sales_commission_fx
FOR EACH ROW EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();
