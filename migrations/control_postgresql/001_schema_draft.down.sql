-- TASLAK. Geri alma, saklanan satirlari silmeden semayi etkin addan ayirir.
-- Yalnizca gecici PostgreSQL 18 dogrulamasi icin.
ALTER SCHEMA ernasa_control RENAME TO ernasa_control_rollback;
