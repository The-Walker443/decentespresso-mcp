"""Waechter ueber dem Archiv (SPEC ss20.7).

Vier Regeln, die nach jedem Bezug pruefen, ob etwas nicht zusammenpasst. Der
Zweck ist nicht Vollstaendigkeit, sondern die kleine Zahl von Fehlern, die man
beim Kaffeemachen tatsaechlich macht und erst Wochen spaeter bemerkt: eine neue
Bohne im alten Mahlgrad gezogen, eine Charge, die laengst durch ist, eine Dosis
danebengewogen, eine Bewertung nie nachgetragen.

GRUNDSAETZE

*Reine Funktionen.* Jede Regel bekommt Zeilen und gibt Befunde zurueck - kein
Netz, keine Datenbank, keine Uhr ausser der uebergebenen. So sind sie ohne
Aufbau pruefbar, und ein Befund laesst sich im Zweifel von Hand nachrechnen.

*Keine Inhalte in Befunden.* Ein Befund nennt Kennung, Regel und Zahlen - nie
den Text einer Notiz. Die Befunde gehen per ntfy aus dem Haus; was dort steht,
liegt danach auf einem fremden Server.

*Jede Regel einzeln abschaltbar.* Eine Regel, die zu oft anschlaegt, wird sonst
im Ganzen ignoriert - und nimmt die anderen mit.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

#: Kennungen der Regeln. Ueber sie laeuft das Abschalten.
RULE_GRIND = "grind_not_adjusted"
RULE_BEAN_AGE = "bean_age"
RULE_MISSING_RATING = "missing_rating"
RULE_DOSE_OUTLIER = "dose_outlier"

ALL_RULES = (RULE_GRIND, RULE_BEAN_AGE, RULE_MISSING_RATING, RULE_DOSE_OUTLIER)

#: Ab wann eine Bohne als ueber ihren Punkt hinaus gilt. Entkoffeinierte und
#: helle Roestungen halten laenger, aber eine Zahl muss es sein; sie ist
#: absichtlich grosszuegig, damit die Regel nicht staendig anschlaegt.
BEAN_AGE_WARN_DAYS = 42

#: So lange nach dem Bezug ist eine fehlende Bewertung normal - man trinkt ja
#: erst. Danach wird sie vermutlich nicht mehr kommen.
RATING_GRACE_HOURS = 36

#: Und so lange danach hat es noch Zweck zu erinnern. Am Bestand gemessen
#: (2026-09-14): 145 von 169 Bezuegen sind unbewertet - ohne Obergrenze
#: meldete die Regel 83 Prozent des Archivs und waere damit wertlos. Wer
#: einen Bezug von vorletzter Woche nicht bewertet hat, tut es nicht mehr.
RATING_WINDOW_HOURS = 7 * 24

#: Abweichung vom Soll, ab der es kein Streuen mehr ist. Gilt fuer das
#: Bezugsgewicht; die Dosis wird gesondert geprueft (siehe unten).
DOSE_TOLERANCE_G = 1.0

#: Faktor auf die Toleranz fuer das Bezugsgewicht. Es schwankt von Natur aus
#: staerker als die Dosis - am Bestand im Mittel 8,9 g -, weil Abbruch von
#: Hand, Tropfen und Kanalbildung hineinspielen. Ohne den Faktor meldete die
#: Regel fast jeden Bezug.
YIELD_TOLERANCE_FACTOR = 4.0

#: Dosiswerte ausserhalb davon sind keine Abweichung, sondern ein Fehler -
#: meist eine nicht tarierte oder gar nicht verbundene Waage.
DOSE_PLAUSIBLE_G = (5.0, 30.0)

#: So viele Bezuege der gleichen Charge braucht es, bevor die Streuung selbst
#: als Massstab taugt.
DOSE_MIN_SAMPLES = 5


@dataclass(frozen=True, slots=True)
class Finding:
    """Ein Befund. ``message`` ist fuer Menschen und enthaelt nie Freitext."""

    rule: str
    shot_id: str
    started_at: str | None
    message: str
    #: Zahlen, auf die sich die Meldung stuetzt - macht sie nachrechenbar.
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "shot_id": self.shot_id,
            "started_at": self.started_at,
            "message": self.message,
            "detail": self.detail,
        }


# --------------------------------------------------------------- Regel 1


def grind_not_adjusted(
    shots: Sequence[Mapping[str, Any]], *, limit: int | None = None
) -> list[Finding]:
    """Neue Bohne oder Charge, aber der Mahlgrad blieb stehen.

    Jede Bohne mahlt anders; wer die Charge wechselt und die Muehle nicht
    anfasst, zieht den ersten Bezug fast sicher daneben. Die Regel meldet
    genau den einen Bezug, bei dem der Wechsel stattfand.

    ``shots`` muss nach ``started_at`` aufsteigend sortiert sein.
    """
    findings: list[Finding] = []
    previous: Mapping[str, Any] | None = None

    for shot in shots:
        batch = shot.get("bean_batch_id")
        grind = shot.get("grinder_setting")
        if previous is not None:
            before_batch = previous.get("bean_batch_id")
            changed = batch is not None and before_batch is not None and batch != before_batch
            if changed and grind == previous.get("grinder_setting"):
                findings.append(Finding(
                    rule=RULE_GRIND,
                    shot_id=str(shot["id"]),
                    started_at=shot.get("started_at"),
                    message=(
                        f"Chargenwechsel ohne Mahlgradaenderung - weiterhin "
                        f"{grind!r}."
                    ),
                    detail={"grinder_setting": grind,
                            "previous_shot": str(previous["id"])},
                ))
        previous = shot

    return _cap(findings, limit)


# --------------------------------------------------------------- Regel 2


def bean_age_days(
    batch: Mapping[str, Any], at: datetime, *, started: datetime | None = None
) -> tuple[int | None, bool]:
    """Alter der Bohne in Tagen, ohne die Gefrierzeit. ``(tage, sicher)``.

    Eingefroren zaehlt nicht als Alterung, deshalb wird die Gefrierzeit
    abgezogen. Decaid fuehrt allerdings **kein Auftaudatum** (am 2026-09-14
    gegen die API geprueft): steht ``frozen`` auf false und ist trotzdem ein
    ``freezeDate`` gesetzt, wurde die Charge irgendwann aufgetaut - wann, weiss
    niemand. Dann ist das Alter nur nach oben begrenzt, und das zweite Element
    der Rueckgabe ist ``False``. Eine Zahl, die man nicht belegen kann, wird
    hier nicht als sicher ausgegeben.
    """
    roasted = _as_date(batch.get("roast_date"))
    if roasted is None:
        return None, False

    reference = (started or at).date()
    age = (reference - roasted).days
    if age < 0:
        return None, False

    frozen_since = _as_date(batch.get("freeze_date"))
    thawed = _as_date(batch.get("unfreeze_date"))
    is_frozen = bool(batch.get("frozen"))

    if frozen_since is None:
        return age, True

    if is_frozen:
        # Seit dem Einfrieren altert sie nicht mehr.
        return max(0, (min(frozen_since, reference) - roasted).days), True

    if thawed is not None:
        return max(0, age - max(0, (thawed - frozen_since).days)), True

    # Eingefroren gewesen, Auftaudatum unbekannt: das volle Alter ist die
    # Obergrenze, mehr laesst sich nicht sagen.
    return age, False


def stale_beans(
    shots: Sequence[Mapping[str, Any]],
    batches: Mapping[str, Mapping[str, Any]],
    *,
    at: datetime,
    warn_days: int = BEAN_AGE_WARN_DAYS,
    limit: int | None = None,
) -> list[Finding]:
    """Bezuege aus einer Charge, die zum Bezugszeitpunkt zu alt war."""
    findings: list[Finding] = []
    for shot in shots:
        batch = batches.get(str(shot.get("bean_batch_id") or ""))
        if batch is None:
            continue
        started = _as_datetime(shot.get("started_at"))
        age, certain = bean_age_days(batch, at, started=started)
        if age is None or age <= warn_days:
            continue

        qualifier = "" if certain else " (mindestens; Auftaudatum unbekannt)"
        findings.append(Finding(
            rule=RULE_BEAN_AGE,
            shot_id=str(shot["id"]),
            started_at=shot.get("started_at"),
            message=(
                f"Bohne war beim Bezug {age} Tage nach der Roestung{qualifier} - "
                f"Schwelle {warn_days}."
            ),
            detail={"age_days": age, "certain": certain, "warn_days": warn_days,
                    "roast_date": batch.get("roast_date")},
        ))
    return _cap(findings, limit)


# --------------------------------------------------------------- Regel 3


def missing_rating(
    shots: Sequence[Mapping[str, Any]],
    *,
    at: datetime,
    grace_hours: int = RATING_GRACE_HOURS,
    window_hours: int = RATING_WINDOW_HOURS,
    limit: int | None = None,
) -> list[Finding]:
    """Bezuege ohne Bewertung - aber nur die, bei denen Erinnern noch hilft.

    Zwei Grenzen, keine offene Frist. Frueher als ``grace_hours`` ist eine
    fehlende Bewertung normal, man trinkt ja erst. Aelter als
    ``window_hours`` kommt sie nicht mehr: am Bestand sind 145 von 169
    Bezuegen unbewertet, eine einseitige Frist meldete also 83 Prozent des
    Archivs - und eine Regel, die fast immer anschlaegt, wird im Ganzen
    ignoriert.

    ``enjoyment IS NULL`` heisst "nicht bewertet" - dass die Ingestion die
    Nullen der Import-Aera zu NULL macht, ist genau die Voraussetzung dafuer,
    dass diese Regel etwas aussagt (SPEC ss20.4).
    """
    newest = at - timedelta(hours=grace_hours)
    oldest = at - timedelta(hours=window_hours)
    findings: list[Finding] = []
    for shot in shots:
        if shot.get("enjoyment") is not None:
            continue
        started = _as_datetime(shot.get("started_at"))
        if started is None or not oldest <= started <= newest:
            continue
        findings.append(Finding(
            rule=RULE_MISSING_RATING,
            shot_id=str(shot["id"]),
            started_at=shot.get("started_at"),
            message=(
                f"Seit {_hours_between(started, at)} h nicht bewertet "
                f"(Frist {grace_hours} h)."
            ),
            detail={"grace_hours": grace_hours, "window_hours": window_hours},
        ))
    return _cap(findings, limit)


# --------------------------------------------------------------- Regel 4


def dose_outliers(
    shots: Sequence[Mapping[str, Any]],
    *,
    tolerance_g: float = DOSE_TOLERANCE_G,
    limit: int | None = None,
) -> list[Finding]:
    """Gewichte, die nicht zum Soll des Workflows passen.

    **Warum das Bezugsgewicht und nicht die Dosis.** Der Auftrag nennt
    Dosis-Ausreisser. Am Bestand gemessen (2026-09-14) ist das nicht
    pruefbar: in allen 165 Faellen ist ``actualDoseWeight`` **exakt** gleich
    ``targetDoseWeight``. Die DE1 wiegt die Dosis nicht; sie uebernimmt den
    Sollwert. Ein Vergleich beider verglich eine Zahl mit sich selbst.

    Die Streuung steckt im Bezugsgewicht - dort misst die Waage wirklich,
    im Mittel 8,9 g neben dem Soll, im Extremfall 497 g. Diese Regel prueft
    deshalb beides mit dem, was es hergibt: das Bezugsgewicht gegen sein
    Soll, und die Dosis nur noch auf Plausibilitaet.

    Fehlt das Soll, dient der Median der Charge als Ersatz - aber erst ab
    genug Bezuegen, sonst bestimmte ein einzelner Fehlgriff den Massstab.
    """
    findings: list[Finding] = []
    medians = _median_per_batch(shots, "yield_g")
    yield_tolerance = tolerance_g * YIELD_TOLERANCE_FACTOR
    low, high = DOSE_PLAUSIBLE_G

    for shot in shots:
        # Die Dosis ist der Sollwert - nur ein unmoeglicher Wert ist ein Befund.
        dose = _as_float(shot.get("dose_g"))
        if dose is not None and not low <= dose <= high:
            findings.append(Finding(
                rule=RULE_DOSE_OUTLIER,
                shot_id=str(shot["id"]),
                started_at=shot.get("started_at"),
                message=f"Dosis {dose:g} g ist unmoeglich - Waage pruefen.",
                detail={"dose_g": dose, "plausible_g": list(DOSE_PLAUSIBLE_G)},
            ))
            continue

        yielded = _as_float(shot.get("yield_g"))
        if yielded is None:
            continue
        target = _as_float(shot.get("target_yield_g"))
        source = "Soll"
        if target is None:
            target = medians.get(str(shot.get("bean_batch_id") or ""))
            source = "Median der Charge"
        if target is None:
            continue

        delta = round(yielded - target, 2)
        if abs(delta) <= yield_tolerance:
            continue
        findings.append(Finding(
            rule=RULE_DOSE_OUTLIER,
            shot_id=str(shot["id"]),
            started_at=shot.get("started_at"),
            message=(
                f"Bezugsgewicht {yielded:g} g weicht um {delta:+g} g vom "
                f"{source} ({target:g} g) ab."
            ),
            detail={"yield_g": yielded, "target_g": target, "delta_g": delta,
                    "basis": source, "tolerance_g": yield_tolerance},
        ))
    return _cap(findings, limit)


# --------------------------------------------------------------- Alle


def run_rules(
    shots: Sequence[Mapping[str, Any]],
    batches: Mapping[str, Mapping[str, Any]],
    *,
    at: datetime,
    enabled: Sequence[str] = ALL_RULES,
    warn_days: int = BEAN_AGE_WARN_DAYS,
    grace_hours: int = RATING_GRACE_HOURS,
    window_hours: int = RATING_WINDOW_HOURS,
    tolerance_g: float = DOSE_TOLERANCE_G,
    limit: int | None = None,
) -> list[Finding]:
    """Alle eingeschalteten Regeln, Befunde nach Bezugszeit absteigend.

    ``shots`` kommt aufsteigend sortiert herein - Regel 1 braucht die Reihenfolge.
    """
    active = set(enabled)
    findings: list[Finding] = []

    if RULE_GRIND in active:
        findings += grind_not_adjusted(shots)
    if RULE_BEAN_AGE in active:
        findings += stale_beans(shots, batches, at=at, warn_days=warn_days)
    if RULE_MISSING_RATING in active:
        findings += missing_rating(shots, at=at, grace_hours=grace_hours,
                                   window_hours=window_hours)
    if RULE_DOSE_OUTLIER in active:
        findings += dose_outliers(shots, tolerance_g=tolerance_g)

    findings.sort(key=lambda f: (f.started_at or "", f.rule), reverse=True)
    return _cap(findings, limit)


# ------------------------------------------------------------ Hilfsmittel


def _median_per_batch(
    shots: Sequence[Mapping[str, Any]], column: str,
) -> dict[str, float]:
    per_batch: dict[str, list[float]] = {}
    for shot in shots:
        value = _as_float(shot.get(column))
        batch = str(shot.get("bean_batch_id") or "")
        if value is not None and batch:
            per_batch.setdefault(batch, []).append(value)
    return {
        batch: statistics.median(values)
        for batch, values in per_batch.items()
        if len(values) >= DOSE_MIN_SAMPLES
    }


def _cap(findings: list[Finding], limit: int | None) -> list[Finding]:
    return findings if limit is None else findings[:limit]


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _as_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hours_between(earlier: datetime, later: datetime) -> int:
    return max(0, int((later - earlier).total_seconds() // 3600))
