CREATE TABLE IF NOT EXISTS ernasa_control.billing_quotes (
    id TEXT PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES ernasa_control.companies(id),
    product TEXT NOT NULL CHECK (product IN ('hr_flow', 'education')),
    plan_name TEXT NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL CHECK (period_end > period_start),
    monthly_usd NUMERIC(12, 2) NOT NULL CHECK (monthly_usd > 0),
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    positions INTEGER CHECK (positions > 0),
    hr_users INTEGER CHECK (hr_users > 0),
    exchange_rate NUMERIC(20, 8) NOT NULL CHECK (exchange_rate > 0),
    rate_date DATE NOT NULL,
    rate_source TEXT NOT NULL,
    rate_type TEXT NOT NULL,
    vat_rate NUMERIC(9, 8) NOT NULL CHECK (vat_rate BETWEEN 0 AND 1),
    currency TEXT NOT NULL DEFAULT 'TRY' CHECK (currency = 'TRY'),
    net_minor BIGINT NOT NULL CHECK (net_minor > 0),
    vat_minor BIGINT NOT NULL CHECK (vat_minor >= 0),
    gross_minor BIGINT NOT NULL CHECK (gross_minor = net_minor + vat_minor),
    created_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS billing_quotes_company_period_idx
ON ernasa_control.billing_quotes (company_id, product, period_start);

CREATE OR REPLACE TRIGGER billing_quotes_immutable
BEFORE UPDATE OR DELETE ON ernasa_control.billing_quotes
FOR EACH ROW EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();

CREATE OR REPLACE TRIGGER billing_quotes_no_truncate
BEFORE TRUNCATE ON ernasa_control.billing_quotes
FOR EACH STATEMENT EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();
