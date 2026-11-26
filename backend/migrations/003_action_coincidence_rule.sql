-- Adds the action_coincidence quarantine rule to the allowed set.
--
-- 002 is edited in place as well, for databases created fresh. This migration
-- exists for a database that already applied 002: the Postgres entrypoint only
-- runs migrations on an empty data directory, so an existing volume never sees
-- the edit.
--
-- The rule: a split and a cash dividend sharing an ex-date is the one case the
-- dividend-unit spike could not test, so the return for that day rests on an
-- unverified assumption and is flagged rather than trusted.

BEGIN;

ALTER TABLE quarantine DROP CONSTRAINT IF EXISTS quarantine_rule_known;

ALTER TABLE quarantine ADD CONSTRAINT quarantine_rule_known CHECK (rule IN (
    'abs_log_return',
    'split_shaped_move',
    'calendar_gap',
    'action_coincidence'
));

COMMIT;
