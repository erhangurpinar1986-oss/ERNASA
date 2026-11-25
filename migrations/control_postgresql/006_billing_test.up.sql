-- Additive: separates founder live-payment tests from real revenue and payable commissions.
ALTER TABLE ernasa_control.billing_payment_requests ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;

-- What the real Sales Hub booking code computed for a test payment. The Sales Hub rows themselves
-- are rolled back, so nothing here is revenue or a payable commission.
CREATE TABLE IF NOT EXISTS ernasa_control.billing_test_results (
    checkout_id TEXT PRIMARY KEY REFERENCES ernasa_control.billing_checkouts(id),
    provider_order_id TEXT NOT NULL UNIQUE,
    verified_at BIGINT NOT NULL,
    gross_minor BIGINT NOT NULL,
    capacity INTEGER NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    sales_net_minor BIGINT,
    sales_rate INTEGER,
    sales_commission_minor BIGINT,
    commission_window TEXT,
    sales_detail TEXT NOT NULL
);
