-- Rollback for 007 only. Drops ONLY objects created by 007; never touches pre-existing tables.
-- Export first (pg_dump -t 'ernasa_control.billing_bank_*') if these tables hold real data.
DROP TRIGGER IF EXISTS billing_bank_checkout_details_immutable ON ernasa_control.billing_bank_checkout_details;
DROP TABLE IF EXISTS ernasa_control.billing_bank_checkout_details;
DROP TABLE IF EXISTS ernasa_control.billing_bank_settings;
