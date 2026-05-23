from dataclasses import dataclass
from datetime import date, timedelta

from .config import (
    COHORT_WEIGHTS,
    NAERINGSKODE_WHITELIST_PREFIXES,
    NEW_BUSINESS_LOOKBACK_DAYS,
    RECENTLY_MOVED_LOOKBACK_DAYS,
)


@dataclass
class EnhetSnapshot:
    orgnr: str
    organisasjonsform: str
    hjemmeside: str | None
    naeringskode1_kode: str | None
    konkurs: bool
    under_avvikling: bool
    slettedato: str | None
    last_oppdatering_dato: str | None  # ISO date of most recent /oppdateringer event
    epost: str | None = None
    registreringsdato: str | None = None


def has_no_website(e: EnhetSnapshot) -> bool:
    s = (e.hjemmeside or "").strip()
    return s == ""


def in_target_industry(e: EnhetSnapshot) -> bool:
    code = (e.naeringskode1_kode or "").strip()
    if not code:
        return False
    return any(code.startswith(p) for p in NAERINGSKODE_WHITELIST_PREFIXES)


def _registered_within(registreringsdato: str | None, today: date, days: int) -> bool:
    if not registreringsdato:
        return False
    try:
        d = date.fromisoformat(registreringsdato[:10])
    except ValueError:
        return False
    return (today - d) <= timedelta(days=days)


def recently_moved(e: EnhetSnapshot, today: date) -> bool:
    if not e.last_oppdatering_dato:
        return False
    try:
        d = date.fromisoformat(e.last_oppdatering_dato[:10])
    except ValueError:
        return False
    return (today - d) <= timedelta(days=RECENTLY_MOVED_LOOKBACK_DAYS)


def classify(
    e: EnhetSnapshot,
    enk_conversion: bool,
    today: date | None = None,
) -> tuple[list[str], int]:
    today = today or date.today()
    if e.organisasjonsform not in ("AS", "ENK"):
        return [], 0
    if e.konkurs or e.under_avvikling or e.slettedato:
        return [], 0
    if e.organisasjonsform == "ENK" and not _registered_within(
        e.registreringsdato, today, NEW_BUSINESS_LOOKBACK_DAYS
    ):
        return [], 0

    cohorts: list[str] = []
    if e.epost and e.epost.strip():
        cohorts.append("reachable")
    if has_no_website(e):
        cohorts.append("no_website")
    if in_target_industry(e):
        cohorts.append("target_industry")
    if enk_conversion:
        cohorts.append("enk_conversion")
    if recently_moved(e, today):
        cohorts.append("recently_moved")

    score = sum(COHORT_WEIGHTS.get(c, 0) for c in cohorts)
    return cohorts, score
