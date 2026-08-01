-- 002: semantic_hash gruppiert Profilversionen, die identisch bruehen.
--
-- Anlass: in den Echtdaten unterschieden sich zwei Versionen des Default-Profils
-- nur durch zwei leere Zusatzschluessel und ein doppeltes Leerzeichen in den
-- Notizen. version_hash bleibt die Identitaet einer Version (SPEC ss5);
-- semantic_hash dient allein der Gruppierung in Auswertungen.
--
-- NULL bedeutet: das Profil war nicht parsebar (parse_ok=false), es gibt also
-- keine semantische Sicht darauf.

ALTER TABLE profiles ADD COLUMN semantic_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_profiles_semantic ON profiles(semantic_hash);
