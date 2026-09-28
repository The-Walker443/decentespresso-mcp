-- Aligning with Beanie (SPEC §11.6; M14).
--
-- A recipe is a bean's most recent real shot - the same thing Beanie restores
-- with a tap - derived from the archive when asked for. The stored per-bean
-- recipes of 0.13 are therefore dropped: everything in them is either in the
-- shots or was only there to be projected into DYE2's list, which this server
-- no longer writes.

DROP TABLE IF EXISTS bean_recipes;
