-- 11.1 diagnosis (T11.1.1 / T11.1.3): row COUNTS and the schema version only — never row
-- contents, emails, hashes or keys. Read-only. Output: one "name|value" line per item.
--
--   Azure: scripts/azure/db-tunnel.sh --password-from-app -- psql -X -At -f scripts/azure/sql/diag_counts.sql
--   Local: psql -X -At -f scripts/azure/sql/diag_counts.sql   (against a throwaway copy)
\set ON_ERROR_STOP on
\set QUIET on
SET default_transaction_read_only = on;
SELECT 'server_version', current_setting('server_version');
SELECT 'alembic_version', coalesce(string_agg(version_num, ','), '(none)') FROM alembic_version;
SELECT 'users', count(*) FROM users;
SELECT 'user_keys', count(*) FROM user_keys;
SELECT 'users_without_key', count(*) FROM users u
  WHERE NOT EXISTS (SELECT 1 FROM user_keys k WHERE k.user_id = u.id);
SELECT 'conversations', count(*) FROM conversations;
SELECT 'messages', count(*) FROM messages;
SELECT 'deletion_requests', count(*) FROM deletion_requests;
SELECT 'email_verification_tokens', count(*) FROM email_verification_tokens;
SELECT 'documents', count(*) FROM documents;
SELECT 'chunks', count(*) FROM chunks;
-- sessions (0006, DEBT-031 / DEC-009 = B): guarded with \if/\gset (not a plain SELECT ...
-- FROM sessions, which Postgres would refuse to even PARSE on a database still on 0005 --
-- a CASE/to_regclass guard inside one statement does not help, since the planner resolves
-- the table reference before any condition is evaluated). Works unmodified on both the
-- live, not-yet-promoted Azure database (0005, no `sessions` table yet) and 0006+.
SELECT (to_regclass('sessions') IS NOT NULL) AS has_sessions
\gset
\if :has_sessions
SELECT 'sessions', count(*) FROM sessions;
\else
SELECT 'sessions', '(table not present yet - pre-0006)';
\endif
