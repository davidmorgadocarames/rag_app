-- SecRAG database roles (T11.0.13). Plain SQL, idempotent: running it twice changes nothing.
--
-- Run it as the database OWNER (the role that runs Alembic) BEFORE every migrate: compose
-- (db-roles service), CI, the gate project, the test DB harness and, later, k3d and the
-- Azure pre-merge. `ALTER DEFAULT PRIVILEGES` applies to objects the executing role creates
-- later, so it must be the same role that runs the migrations (W16: every future table
-- stays dumpable by secrag_backup without a new grant).
--
-- Roles are created NOLOGIN and without a password. LOGIN + password are set OUTSIDE Alembic
-- and outside this file (scripts/db/apply_roles.sh reads them from the environment: the gate's
-- git-ignored .gate/roles.env locally, generated secrets on Azure). This file never touches an
-- existing role's attributes, so re-running it never undoes a password or a LOGIN.
--
-- Table-level grants per role live in the migrations that create the tables (X4 migration
-- map; e.g. 0005 grants the purger its DELETE/UPDATE rights). Roles added in later phases
-- (worker, retention, analyst_ro, …) are appended here, NOLOGIN.

DO $roles$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['secrag_purger', 'secrag_backup'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO secrag_purger, secrag_backup', current_database()
    );
END
$roles$;

GRANT USAGE ON SCHEMA public TO secrag_purger, secrag_backup;

-- secrag_backup: read-only, everything pg_dump needs — existing objects now, future objects
-- created by this (owner) role through default privileges.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO secrag_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO secrag_backup;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO secrag_backup;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO secrag_backup;
