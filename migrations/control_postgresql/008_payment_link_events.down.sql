-- Rollback for 008 only: drops only the delivery history table created by 008.
DROP TRIGGER IF EXISTS billing_link_events_immutable ON ernasa_control.billing_link_events;
DROP TABLE IF EXISTS ernasa_control.billing_link_events;
