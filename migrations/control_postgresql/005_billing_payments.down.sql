-- Rollback for 005 only. Drops ONLY tables created by 005; never touches pre-existing tables.
-- Run only after exporting these tables (pg_dump -t 'ernasa_control.billing_*') if they hold real data.
DROP TABLE IF EXISTS ernasa_control.billing_usage;
DROP TABLE IF EXISTS ernasa_control.billing_periods;
DROP TABLE IF EXISTS ernasa_control.billing_provider_orders;
DROP TABLE IF EXISTS ernasa_control.billing_subscriptions;
DROP TABLE IF EXISTS ernasa_control.billing_webhook_events;
DROP TABLE IF EXISTS ernasa_control.billing_audit;
DROP TABLE IF EXISTS ernasa_control.billing_checkouts;
