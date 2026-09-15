"""Guards over the archive (SPEC §10).

Four rules that check after every shot whether something does not add up. The
aim is not completeness but the small set of mistakes one actually makes while
making coffee and only notices weeks later: a new bean pulled at the old grind
setting, a batch long past its prime, a dose weighed wrong, a rating never
added.

PRINCIPLES

*Pure functions.* Every rule takes rows and returns findings - no network, no
database, no clock beyond the one handed in. That makes them testable without
any setup, and a finding can be recomputed by hand when in doubt.

*No content in findings.* A finding names the identifier, the rule and the
numbers - never the text of a note. Findings leave the house over ntfy; what
is in them then sits on someone else's server.

*Every rule switchable on its own.* A rule that fires too often gets ignored
wholesale otherwise - and takes the others down with it.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

#: Rule identifiers. Switching rules off works through these.
RULE_GRIND = "grind_not_adjusted"
RULE_BEAN_AGE = "bean_age"
RULE_MISSING_RATING = "missing_rating"
RULE_DOSE_OUTLIER = "dose_outlier"

ALL_RULES = (RULE_GRIND, RULE_BEAN_AGE, RULE_MISSING_RATING, RULE_DOSE_OUTLIER)

#: When a bean counts as past its prime. Decaf and light roasts keep longer,
#: but it has to be a single number; it is deliberately generous so the rule
#: does not fire constantly.
BEAN_AGE_WARN_DAYS = 42

#: For this long after a shot a missing rating is normal - one drinks it
#: first. After that it probably is not coming.
RATING_GRACE_HOURS = 36

#: And for this long afterwards a reminder still has a point. Measured against
#: the archive (2026-09-14): 145 of 169 shots are unrated - without an upper
#: bound the rule reported 83 percent of the archive and would be worthless.
#: Whoever has not rated a shot from the week before last will not do so now.
RATING_WINDOW_HOURS = 7 * 24

#: Deviation from target beyond which it is no longer scatter. Applies to the
#: yield; the dose is checked separately (see below).
DOSE_TOLERANCE_G = 1.0

#: Factor applied to the tolerance for yield. It varies more by nature than
#: the dose - 8.9 g on average across the archive - because stopping by hand,
#: dripping and channelling all play into it. Without the factor the rule
#: reported nearly every shot.
YIELD_TOLERANCE_FACTOR = 4.0

#: Dose values outside this are not a deviation but a fault - usually a scale
#: that was not tared or not connected at all.
DOSE_PLAUSIBLE_G = (5.0, 30.0)

#: How many shots from the same batch are needed before their own scatter
#: makes a usable yardstick.
DOSE_MIN_SAMPLES = 5


@dataclass(frozen=True, slots=True)
class Finding:
    """One finding. ``message`` is for humans and never carries free text."""

    rule: str
    shot_id: str
    started_at: str | None
    message: str
    #: The numbers the message rests on - they make it checkable.
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "shot_id": self.shot_id,
            "started_at": self.started_at,
            "message": self.message,
            "detail": self.detail,
        }


# ---------------------------------------------------------------- Rule 1


def grind_not_adjusted(
    shots: Sequence[Mapping[str, Any]], *, limit: int | None = None
) -> list[Finding]:
    """New bean or batch, but the grind setting stayed put.

    Every bean grinds differently; changing the batch without touching the
    grinder almost certainly puts the first shot off. The rule reports exactly
    the one shot at which the change happened.

    ``shots`` must be sorted ascending by ``started_at``.
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
                        f"Batch changed without adjusting the grind - still "
                        f"{grind!r}."
                    ),
                    detail={"grinder_setting": grind,
                            "previous_shot": str(previous["id"])},
                ))
        previous = shot

    return _cap(findings, limit)


# ---------------------------------------------------------------- Rule 2


def bean_age_days(
    batch: Mapping[str, Any], at: datetime, *, started: datetime | None = None
) -> tuple[int | None, bool]:
    """Age of the bean in days, excluding time spent frozen. ``(days, certain)``.

    Being frozen does not count as ageing, so frozen time is subtracted. Decaid
    however keeps **no thaw date** (checked against the API on 2026-09-14): if
    ``frozen`` is false and a ``freezeDate`` is set nonetheless, the batch was
    thawed at some point - nobody knows when. The age is then an upper bound
    only, and the second element of the return value is ``False``. A number
    that cannot be substantiated is not reported here as certain.
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
        # It stops ageing the moment it goes into the freezer.
        return max(0, (min(frozen_since, reference) - roasted).days), True

    if thawed is not None:
        return max(0, age - max(0, (thawed - frozen_since).days)), True

    # Was frozen, thaw date unknown: the full age is the upper bound, and
    # nothing more can be said.
    return age, False


def stale_beans(
    shots: Sequence[Mapping[str, Any]],
    batches: Mapping[str, Mapping[str, Any]],
    *,
    at: datetime,
    warn_days: int = BEAN_AGE_WARN_DAYS,
    limit: int | None = None,
) -> list[Finding]:
    """Shots from a batch that was too old at the time it was pulled."""
    findings: list[Finding] = []
    for shot in shots:
        batch = batches.get(str(shot.get("bean_batch_id") or ""))
        if batch is None:
            continue
        started = _as_datetime(shot.get("started_at"))
        age, certain = bean_age_days(batch, at, started=started)
        if age is None or age <= warn_days:
            continue

        qualifier = "" if certain else " (at least; thaw date unknown)"
        findings.append(Finding(
            rule=RULE_BEAN_AGE,
            shot_id=str(shot["id"]),
            started_at=shot.get("started_at"),
            message=(
                f"Bean was {age} days past roasting when pulled{qualifier} - "
                f"threshold {warn_days}."
            ),
            detail={"age_days": age, "certain": certain, "warn_days": warn_days,
                    "roast_date": batch.get("roast_date")},
        ))
    return _cap(findings, limit)


# ---------------------------------------------------------------- Rule 3


def missing_rating(
    shots: Sequence[Mapping[str, Any]],
    *,
    at: datetime,
    grace_hours: int = RATING_GRACE_HOURS,
    window_hours: int = RATING_WINDOW_HOURS,
    limit: int | None = None,
) -> list[Finding]:
    """Shots without a rating - but only those where a reminder still helps.

    Two bounds, not an open-ended deadline. Earlier than ``grace_hours`` a
    missing rating is normal, one drinks it first. Older than ``window_hours``
    it is not coming: 145 of 169 shots in the archive are unrated, so a
    one-sided deadline reported 83 percent of it - and a rule that fires almost
    always gets ignored wholesale.

    ``enjoyment IS NULL`` means "not rated" - that ingestion turns the zeros of
    the import era into NULL is exactly what makes this rule say anything at
    all (SPEC §5).
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
                f"Not rated for {_hours_between(started, at)} h "
                f"(grace period {grace_hours} h)."
            ),
            detail={"grace_hours": grace_hours, "window_hours": window_hours},
        ))
    return _cap(findings, limit)


# ---------------------------------------------------------------- Rule 4


def dose_outliers(
    shots: Sequence[Mapping[str, Any]],
    *,
    tolerance_g: float = DOSE_TOLERANCE_G,
    limit: int | None = None,
) -> list[Finding]:
    """Weights that do not match the workflow target.

    **Why yield and not dose.** The brief asks for dose outliers. Measured
    against the archive (2026-09-14) that cannot be checked: in all 165 cases
    ``actualDoseWeight`` is **exactly** equal to ``targetDoseWeight``. The DE1
    does not weigh the dose; it adopts the target. Comparing the two compared a
    number with itself.

    The scatter sits in the yield - that is where the scale really measures,
    8.9 g off target on average and 497 g in the extreme. This rule therefore
    checks both with what they can give: the yield against its target, and the
    dose for plausibility only.

    Without a target the batch median stands in - but only once there are
    enough shots, otherwise a single mishap would set the yardstick itself.
    """
    findings: list[Finding] = []
    medians = _median_per_batch(shots, "yield_g")
    yield_tolerance = tolerance_g * YIELD_TOLERANCE_FACTOR
    low, high = DOSE_PLAUSIBLE_G

    for shot in shots:
        # The dose is the target value - only an impossible one is a finding.
        dose = _as_float(shot.get("dose_g"))
        if dose is not None and not low <= dose <= high:
            findings.append(Finding(
                rule=RULE_DOSE_OUTLIER,
                shot_id=str(shot["id"]),
                started_at=shot.get("started_at"),
                message=f"Dose of {dose:g} g is impossible - check the scale.",
                detail={"dose_g": dose, "plausible_g": list(DOSE_PLAUSIBLE_G)},
            ))
            continue

        yielded = _as_float(shot.get("yield_g"))
        if yielded is None:
            continue
        target = _as_float(shot.get("target_yield_g"))
        source = "target"
        if target is None:
            target = medians.get(str(shot.get("bean_batch_id") or ""))
            source = "batch median"
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
                f"Yield of {yielded:g} g is {delta:+g} g off the "
                f"{source} ({target:g} g)."
            ),
            detail={"yield_g": yielded, "target_g": target, "delta_g": delta,
                    "basis": source, "tolerance_g": yield_tolerance},
        ))
    return _cap(findings, limit)


# ----------------------------------------------------------------- All


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
    """Every enabled rule, findings sorted by shot time, newest first.

    ``shots`` comes in sorted ascending - rule 1 depends on that order.
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


# ------------------------------------------------------------------ Helpers


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
