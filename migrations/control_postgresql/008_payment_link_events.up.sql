-- Additive: delivery history of customer payment links (e-mail via the existing provider,
-- WhatsApp click-to-chat opens). Append-only; touches no existing table or row.
CREATE TABLE IF NOT EXISTS ernasa_control.billing_link_events (
    id TEXT PRIMARY KEY,
    ref TEXT NOT NULL REFERENCES ernasa_control.billing_payment_requests(ref),
    channel TEXT NOT NULL CHECK (channel IN ('email', 'whatsapp')),
    status TEXT NOT NULL CHECK (status IN ('sent', 'failed', 'opened')),
    actor_id TEXT NOT NULL,
    recipient TEXT NOT NULL,
    provider_message_id TEXT,
    error TEXT,
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS billing_link_events_ref ON ernasa_control.billing_link_events(ref, created_at);
CREATE OR REPLACE TRIGGER billing_link_events_immutable
BEFORE UPDATE OR DELETE ON ernasa_control.billing_link_events
FOR EACH ROW EXECUTE FUNCTION ernasa_control.sales_reject_history_mutation();
