CREATE TABLE IF NOT EXISTS ernasa_control.billing_bank_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    bank_name TEXT NOT NULL,
    account_holder TEXT NOT NULL,
    iban TEXT NOT NULL CHECK (iban ~ '^TR[0-9]{24}$'),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    updated_by TEXT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS ernasa_control.billing_bank_checkout_details (
    checkout_id TEXT PRIMARY KEY REFERENCES ernasa_control.billing_checkouts(id),
    bank_name TEXT NOT NULL,
    account_holder TEXT NOT NULL,
    iban TEXT NOT NULL,
    created_at BIGINT NOT NULL
);
CREATE OR REPLACE TRIGGER billing_bank_checkout_details_immutable
BEFORE UPDATE OR DELETE ON ernasa_control.billing_bank_checkout_details
FOR EACH ROW EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();
