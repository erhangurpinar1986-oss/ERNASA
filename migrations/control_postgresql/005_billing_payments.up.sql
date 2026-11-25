-- Additive only: no existing table, row or column is altered or removed.
-- HR FLOW / Education payments. ERNASA CV payment tables are deliberately not referenced.

CREATE TABLE IF NOT EXISTS ernasa_control.billing_checkouts (
    id TEXT PRIMARY KEY,
    quote_id TEXT NOT NULL UNIQUE REFERENCES ernasa_control.billing_quotes(id),
    company_id BIGINT NOT NULL REFERENCES ernasa_control.companies(id),
    provider TEXT NOT NULL CHECK (provider IN ('shopier_hr', 'bank_transfer')),
    -- Single-use provider product issued for this checkout only (binds order -> checkout).
    provider_product_id TEXT UNIQUE,
    payment_reference TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'failed', 'expired', 'cancelled', 'refunded')),
    created_at BIGINT NOT NULL,
    CHECK (provider <> 'shopier_hr' OR provider_product_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS ernasa_control.billing_provider_orders (
    provider TEXT NOT NULL CHECK (provider IN ('shopier_hr', 'bank_transfer')),
    provider_order_id TEXT NOT NULL,
    checkout_id TEXT NOT NULL UNIQUE REFERENCES ernasa_control.billing_checkouts(id),
    gross_minor BIGINT NOT NULL CHECK (gross_minor > 0),
    currency TEXT NOT NULL CHECK (currency = 'TRY'),
    verified_at BIGINT NOT NULL,
    verified_by TEXT NOT NULL,
    response_sha256 TEXT NOT NULL,
    PRIMARY KEY (provider, provider_order_id)
);

CREATE TABLE IF NOT EXISTS ernasa_control.billing_subscriptions (
    id TEXT PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES ernasa_control.companies(id),
    product TEXT NOT NULL CHECK (product IN ('hr_flow', 'education')),
    status TEXT NOT NULL CHECK (status IN ('awaiting_payment', 'active', 'renewal_due', 'expired',
                                           'suspended', 'cancelled', 'refunded')),
    created_at BIGINT NOT NULL,
    UNIQUE (company_id, product)
);

-- One row per paid period; the contract price is frozen from the quote.
CREATE TABLE IF NOT EXISTS ernasa_control.billing_periods (
    id TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL REFERENCES ernasa_control.billing_subscriptions(id),
    quote_id TEXT NOT NULL UNIQUE REFERENCES ernasa_control.billing_quotes(id),
    checkout_id TEXT NOT NULL UNIQUE REFERENCES ernasa_control.billing_checkouts(id),
    period_start DATE NOT NULL,
    period_end DATE NOT NULL CHECK (period_end > period_start),
    plan_name TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    positions INTEGER,
    hr_users INTEGER,
    net_minor BIGINT NOT NULL CHECK (net_minor > 0),
    vat_minor BIGINT NOT NULL CHECK (vat_minor >= 0),
    activated_at BIGINT NOT NULL,
    UNIQUE (subscription_id, period_start)
);

CREATE TABLE IF NOT EXISTS ernasa_control.billing_usage (
    period_id TEXT NOT NULL REFERENCES ernasa_control.billing_periods(id),
    subject_key TEXT NOT NULL,          -- e.g. candidate process id; one unit per process
    used_at BIGINT NOT NULL,
    PRIMARY KEY (period_id, subject_key)
);

CREATE TABLE IF NOT EXISTS ernasa_control.billing_webhook_events (
    provider TEXT NOT NULL,
    delivery_sha256 TEXT NOT NULL,
    provider_order_id TEXT,
    received_at BIGINT NOT NULL,
    outcome TEXT NOT NULL,
    PRIMARY KEY (provider, delivery_sha256)
);

CREATE TABLE IF NOT EXISTS ernasa_control.billing_audit (
    id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    target TEXT NOT NULL,
    detail TEXT NOT NULL,
    stamp BIGINT NOT NULL
);

CREATE OR REPLACE TRIGGER billing_provider_orders_immutable
BEFORE UPDATE OR DELETE ON ernasa_control.billing_provider_orders
FOR EACH ROW EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();
CREATE OR REPLACE TRIGGER billing_periods_immutable
BEFORE UPDATE OR DELETE ON ernasa_control.billing_periods
FOR EACH ROW EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();
CREATE OR REPLACE TRIGGER billing_audit_immutable
BEFORE UPDATE OR DELETE ON ernasa_control.billing_audit
FOR EACH ROW EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();

-- Sales Hub link: which approved proposal / representative a checkout belongs to.
-- Direct sales have no row here and therefore never create commission.
CREATE TABLE IF NOT EXISTS ernasa_control.sales_customer_companies (
    customer_id TEXT PRIMARY KEY REFERENCES ernasa_control.sales_customers(id),
    company_id BIGINT NOT NULL UNIQUE REFERENCES ernasa_control.companies(id),
    linked_by BIGINT NOT NULL REFERENCES ernasa_control.users(id),
    linked_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS ernasa_control.billing_checkout_sales (
    checkout_id TEXT PRIMARY KEY REFERENCES ernasa_control.billing_checkouts(id),
    proposal_id TEXT NOT NULL REFERENCES ernasa_control.sales_proposals(id),
    owner_id TEXT NOT NULL,
    sales_collection_id TEXT UNIQUE REFERENCES ernasa_control.sales_collections(id)
);
