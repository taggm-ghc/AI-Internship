-- Migration 007 ROLLBACK -- DESTRUCTIVE: drops internship.licence_observations and every observation in it.
-- Requires R1's separate, same-turn approval and an admin account. Run only after a pg_dump of the table.
-- Non-destructive rollback is: set the config flag off, then REVOKE INSERT from the writer roles.

DROP TABLE IF EXISTS internship.licence_observations;
DROP FUNCTION IF EXISTS internship.licence_observations_forbid_mutation();

DELETE FROM internship.schema_version WHERE version = 7;
