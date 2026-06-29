-- Compose/CI bootstrap: the canonical migration init scripts GRANT privileges to
-- role "mnemos" (the production role name), but this compose's superuser is
-- "mnemos_user". Create the "mnemos" role up front (idempotent) so those GRANTs
-- don't abort initdb with: ERROR: role "mnemos" does not exist.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mnemos') THEN
    CREATE ROLE mnemos;
  END IF;
END $$;
