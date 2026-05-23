# Reachable-first leads (AS + ENK) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make "newly-registered AS or ENK with an email (on Brreg or proff)" the primary lead, pinned to the top of the dashboard; website status only decides the pitch.

**Architecture:** A new `reachable` cohort (has email) is added to the pure `classify` function, which now also accepts ENK (guarded by a new-business recency window so the ~30–80k seeded historical ENKs don't flood the list). Ingest gains new-ENK paging plus proff-enrichment of recent ENKs missing a Brreg email. The dashboard re-sorts reachable-first and gains reachable/orgform/website filters.

**Tech Stack:** Python 3, `sqlite3` (no ORM), `httpx`, FastAPI + Jinja2, `pytest`. All tests are offline.

---

## File structure

- `src/brreg_leads/config.py` — add `reachable` weight + `NEW_BUSINESS_LOOKBACK_DAYS`.
- `src/brreg_leads/classify.py` — accept ENK, recency guard, `reachable` cohort; `EnhetSnapshot` gains `epost`, `registreringsdato`.
- `src/brreg_leads/brreg_client.py` — generalize `iter_new_as` → `iter_new_enheter(organisasjonsform, ...)`.
- `src/brreg_leads/ingest.py` — new-ENK paging, recent-ENK enrichment, reclassify scope + email/regdato/enrichment join.
- `src/brreg_leads/web/routes.py` — reachable-first sort, orgform in query, new filters; count query gains enrichment join.
- `src/brreg_leads/web/templates/leads.html` — AS/ENK badge, reachable/orgform/website filter controls.
- `CLAUDE.md`, `README.md` — reflect AS+ENK + reachable-first.
- Tests: `tests/test_classify.py`, `tests/test_ingest.py`, `tests/test_routes.py`.

Work proceeds on the existing `reachable-first-leads` branch.

---

## Task 1: `reachable` cohort + ENK acceptance in `classify`

**Files:**
- Modify: `src/brreg_leads/config.py:39-50`
- Modify: `src/brreg_leads/classify.py:1-67`
- Test: `tests/test_classify.py`

- [ ] **Step 1: Add config values**

In `src/brreg_leads/config.py`, change `COHORT_WEIGHTS` and add the lookback constant:

```python
COHORT_WEIGHTS: dict[str, int] = {
    "reachable": 5,
    "no_website": 3,
    "target_industry": 2,
    "enk_conversion": 4,
    "recently_moved": 1,
}

LEAD_STATUSES: list[str] = ["new", "contacted", "interested", "won", "lost", "ignored"]

ENK_CONVERSION_AS_LOOKBACK_DAYS = 30
ENK_CONVERSION_ENK_LOOKBACK_DAYS = 60
RECENTLY_MOVED_LOOKBACK_DAYS = 30
NEW_BUSINESS_LOOKBACK_DAYS = 90
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_classify.py`:

```python
def test_reachable_when_email_present():
    cohorts, score = classify(
        _snap(epost="post@firma.no"), enk_conversion=False
    )
    assert "reachable" in cohorts
    assert score >= 5


def test_not_reachable_when_email_blank():
    cohorts, _ = classify(_snap(epost=""), enk_conversion=False)
    assert "reachable" not in cohorts


def test_enk_recent_with_email_qualifies():
    cohorts, _ = classify(
        _snap(
            organisasjonsform="ENK",
            epost="post@enk.no",
            registreringsdato="2026-05-01",
            hjemmeside="",
        ),
        enk_conversion=False,
        today=date(2026, 5, 23),
    )
    assert "reachable" in cohorts
    assert "no_website" in cohorts


def test_enk_old_is_not_a_lead():
    cohorts, score = classify(
        _snap(
            organisasjonsform="ENK",
            epost="post@enk.no",
            registreringsdato="2025-01-01",
            hjemmeside="",
        ),
        enk_conversion=False,
        today=date(2026, 5, 23),
    )
    assert cohorts == []
    assert score == 0


def test_enk_missing_regdato_is_not_a_lead():
    cohorts, _ = classify(
        _snap(organisasjonsform="ENK", epost="post@enk.no", registreringsdato=None),
        enk_conversion=False,
        today=date(2026, 5, 23),
    )
    assert cohorts == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_classify.py -v`
Expected: the new tests FAIL (TypeError: unexpected keyword `epost`/`registreringsdato`, since `EnhetSnapshot` lacks those fields).

- [ ] **Step 4: Implement classify changes**

In `src/brreg_leads/classify.py`, update the import, dataclass, and `classify`:

```python
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
```

Add a helper next to `recently_moved`:

```python
def _registered_within(registreringsdato: str | None, today: date, days: int) -> bool:
    if not registreringsdato:
        return False
    try:
        d = date.fromisoformat(registreringsdato[:10])
    except ValueError:
        return False
    return (today - d) <= timedelta(days=days)
```

Replace the body of `classify` (keep the signature):

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_classify.py -v`
Expected: PASS (including the pre-existing `test_non_as_excluded`, since an ENK with `registreringsdato=None` is rejected by the recency guard).

- [ ] **Step 6: Commit**

```bash
git add src/brreg_leads/config.py src/brreg_leads/classify.py tests/test_classify.py
git commit -m "feat: add reachable cohort and ENK acceptance to classify"
```

---

## Task 2: Generalize the new-entity pager to any organisasjonsform

**Files:**
- Modify: `src/brreg_leads/brreg_client.py:101-129`
- Modify: `src/brreg_leads/ingest.py:234-253` (caller)
- Test: `tests/test_brreg_client.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_brreg_client.py` (follow the existing fixture/mock style in that file; this assumes the existing `respx`/httpx-mock helper used there — match whatever pattern is already present). If the file mocks via a transport, mirror it. Minimal behavioral test:

```python
def test_iter_new_enheter_passes_organisasjonsform(monkeypatch):
    from brreg_leads.brreg_client import BrregClient

    captured = {}

    class _Resp:
        def json(self):
            return {"_embedded": {"enheter": []}, "page": {"totalPages": 0}}

    def fake_get(self, path, params=None):
        captured["params"] = params
        return _Resp()

    monkeypatch.setattr(BrregClient, "_get", fake_get)
    client = BrregClient()
    list(client.iter_new_enheter("ENK", "0301", registered_from="2026-05-01"))
    assert captured["params"]["organisasjonsform"] == "ENK"
    assert captured["params"]["kommunenummer"] == "0301"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_brreg_client.py::test_iter_new_enheter_passes_organisasjonsform -v`
Expected: FAIL with `AttributeError: 'BrregClient' object has no attribute 'iter_new_enheter'`.

- [ ] **Step 3: Rename + generalize the method**

In `src/brreg_leads/brreg_client.py`, replace `iter_new_as` with:

```python
    def iter_new_enheter(
        self,
        organisasjonsform: str,
        kommunenummer: str,
        registered_from: str,
        registered_to: str | None = None,
        page_size: int = 500,
    ) -> Iterator[dict[str, Any]]:
        page = 0
        while True:
            params: dict[str, Any] = {
                "organisasjonsform": organisasjonsform,
                "kommunenummer": kommunenummer,
                "fraRegistreringsdatoEnhetsregisteret": registered_from,
                "size": page_size,
                "page": page,
            }
            if registered_to:
                params["tilRegistreringsdatoEnhetsregisteret"] = registered_to
            data = self._get("/enheter", params=params).json()
            enheter = data.get("_embedded", {}).get("enheter", [])
            if not enheter:
                return
            for e in enheter:
                yield e
            page_info = data.get("page", {})
            total_pages = page_info.get("totalPages", 0)
            page += 1
            if page >= total_pages:
                return
```

- [ ] **Step 4: Update the existing AS caller**

In `src/brreg_leads/ingest.py`, inside `_pull_new_as`, change the iteration line:

```python
        for enhet in client.iter_new_enheter("AS", k, registered_from=since, registered_to=until):
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_brreg_client.py tests/test_ingest.py -v`
Expected: PASS (no remaining references to `iter_new_as`). Grep to confirm: `git grep -n iter_new_as` returns nothing.

- [ ] **Step 6: Commit**

```bash
git add src/brreg_leads/brreg_client.py src/brreg_leads/ingest.py tests/test_brreg_client.py
git commit -m "refactor: generalize iter_new_as to iter_new_enheter"
```

---

## Task 3: Ingest new ENKs + enrich recent ENKs missing a Brreg email

**Files:**
- Modify: `src/brreg_leads/ingest.py` (`IngestSummary` 21-28; add `_pull_new_enk` + `_enrich_recent_enks` near 234-253; wire into `run_ingest` 410-459)
- Test: `tests/test_ingest.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ingest.py` (uses the in-memory `conn` fixture already defined there):

```python
class FakeIngestClient:
    """Pages new enheter and serves no roller; records proff-independent calls."""

    def __init__(self, enheter_by_form: dict[str, list[dict]]):
        self._by_form = enheter_by_form

    def iter_new_enheter(self, organisasjonsform, kommunenummer, registered_from, registered_to=None):
        for e in self._by_form.get(organisasjonsform, []):
            if e.get("forretningsadresse", {}).get("kommunenummer") == kommunenummer:
                yield e


def _enk_payload(orgnr, *, regdato, epost=None, kommune="0301"):
    return {
        "organisasjonsnummer": orgnr,
        "navn": f"ENK {orgnr}",
        "organisasjonsform": {"kode": "ENK"},
        "registreringsdatoEnhetsregisteret": regdato,
        "forretningsadresse": {"kommunenummer": kommune, "kommune": "OSLO"},
        "epostadresse": epost,
    }


def test_pull_new_enk_upserts(conn):
    client = FakeIngestClient({"ENK": [_enk_payload("810000001", regdato="2026-05-20", epost="a@b.no")]})
    seen = ingest._pull_new_enk(client, conn, since="2026-05-01", until=None, kommuner=["0301"])
    assert seen == 1
    row = conn.execute("SELECT organisasjonsform, epost FROM enheter WHERE orgnr='810000001'").fetchone()
    assert row["organisasjonsform"] == "ENK"
    assert row["epost"] == "a@b.no"


def test_enrich_recent_enks_only_targets_missing_email(conn, monkeypatch):
    # one in-window ENK missing email, one with email, one too old
    for orgnr, regdato, epost in [
        ("810000010", "2026-05-20", None),
        ("810000011", "2026-05-20", "has@mail.no"),
        ("810000012", "2024-01-01", None),
    ]:
        ingest.upsert_enhet(conn, _enk_payload(orgnr, regdato=regdato, epost=epost))

    targeted: list[str] = []

    def fake_enrich_orgnrs(c, orgnrs, proff=None, skip_if_brreg_has_email=True):
        targeted.extend(orgnrs)
        return {"attempted": len(targeted), "enriched": 0, "skipped": 0}

    monkeypatch.setattr(ingest.enrich, "enrich_orgnrs", fake_enrich_orgnrs)
    ingest._enrich_recent_enks(conn, today=date(2026, 5, 23), proff=None)
    assert targeted == ["810000010"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py -k "new_enk or recent_enks" -v`
Expected: FAIL with `AttributeError: module 'brreg_leads.ingest' has no attribute '_pull_new_enk'` / `_enrich_recent_enks`.

- [ ] **Step 3: Add the summary field**

In `src/brreg_leads/ingest.py`, add `new_enk_seen` to `IngestSummary`:

```python
@dataclass
class IngestSummary:
    new_as_seen: int = 0
    new_enk_seen: int = 0
    updates_seen: int = 0
    roller_fetched: int = 0
    leads_upserted: int = 0
    enk_conversions: int = 0
    enriched: int = 0
```

- [ ] **Step 4: Add the imports and helper functions**

In `src/brreg_leads/ingest.py`, extend the config import:

```python
from .config import KOMMUNER, NEW_BUSINESS_LOOKBACK_DAYS
```

Add these two functions just below `_pull_new_as`:

```python
def _pull_new_enk(
    client: "BrregClient",
    conn: sqlite3.Connection,
    since: str,
    until: str | None,
    kommuner: Iterable[str],
) -> int:
    seen = 0
    for k in kommuner:
        log.info("Pulling new ENK for kommune %s since %s", k, since)
        for enhet in client.iter_new_enheter("ENK", k, registered_from=since, registered_to=until):
            upsert_enhet(conn, enhet)
            seen += 1
    return seen


def _enrich_recent_enks(
    conn: sqlite3.Connection,
    today: date,
    proff: "enrich.ProffClient | None",
) -> int:
    """Proff-enrich in-window ENKs that have no Brreg email, to discover proff-only
    addresses. Skips ENKs we've already enriched. Idempotent."""
    cutoff = (today - timedelta(days=NEW_BUSINESS_LOOKBACK_DAYS)).isoformat()
    rows = conn.execute(
        """
        SELECT orgnr FROM enheter
        WHERE organisasjonsform = 'ENK'
          AND registreringsdato >= ?
          AND (epost IS NULL OR epost = '')
          AND orgnr NOT IN (SELECT orgnr FROM enrichment)
        """,
        (cutoff,),
    ).fetchall()
    orgnrs = [r["orgnr"] for r in rows]
    if not orgnrs:
        return 0
    summary = enrich.enrich_orgnrs(conn, orgnrs, proff=proff, skip_if_brreg_has_email=False)
    return summary["enriched"]
```

- [ ] **Step 5: Wire into `run_ingest`**

In `src/brreg_leads/ingest.py`, in `run_ingest`, after the `_pull_new_as` block (line ~427-428) add ENK paging:

```python
        seen, new_as_orgnrs = _pull_new_as(client, conn, since, until, target_kommuner)
        summary.new_as_seen = seen
        summary.new_enk_seen = _pull_new_enk(client, conn, since, until, target_kommuner)
```

Then replace the AS-only enrichment block (currently lines ~447-451) with a combined block that enriches both new ASes and recent ENKs under one client:

```python
        with enrich.ProffClient() as proff:
            if new_as_orgnrs:
                log.info("Enriching %d new ASes via proff.no", len(new_as_orgnrs))
                summary.enriched += enrich.enrich_orgnrs(conn, new_as_orgnrs, proff=proff)["enriched"]
            summary.enriched += _enrich_recent_enks(conn, today, proff)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_ingest.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/brreg_leads/ingest.py tests/test_ingest.py
git commit -m "feat: ingest new ENKs and proff-enrich recent ENKs missing email"
```

---

## Task 4: Reclassify ASes + recent ENKs, sourcing email from Brreg or proff

**Files:**
- Modify: `src/brreg_leads/ingest.py:188-231` (`_reclassify_all`)
- Test: `tests/test_ingest.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ingest.py`:

```python
def test_reclassify_includes_recent_enk_excludes_old(conn):
    ingest.upsert_enhet(conn, _enk_payload("820000001", regdato="2026-05-20", epost="x@y.no"))
    ingest.upsert_enhet(conn, _enk_payload("820000002", regdato="2023-01-01", epost="x@y.no"))
    n = ingest._reclassify_all(conn, enk_matches={}, today=date(2026, 5, 23))
    recent = conn.execute("SELECT cohorts_json FROM leads WHERE orgnr='820000001'").fetchone()
    old = conn.execute("SELECT 1 FROM leads WHERE orgnr='820000002'").fetchone()
    assert recent is not None and "reachable" in recent["cohorts_json"]
    assert old is None
    assert n >= 1


def test_reclassify_uses_enrichment_email(conn):
    ingest.upsert_enhet(conn, _enk_payload("830000001", regdato="2026-05-20", epost=None))
    conn.execute(
        "INSERT INTO enrichment (orgnr, epost, source, fetched_at) VALUES (?, ?, 'proff', '2026-05-23')",
        ("830000001", "proffonly@firma.no"),
    )
    ingest._reclassify_all(conn, enk_matches={}, today=date(2026, 5, 23))
    row = conn.execute("SELECT cohorts_json FROM leads WHERE orgnr='830000001'").fetchone()
    assert row is not None and "reachable" in row["cohorts_json"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py -k reclassify -v`
Expected: FAIL — currently `_reclassify_all` only selects `WHERE organisasjonsform = 'AS'` and never sets `epost`/`registreringsdato` on the snapshot, so ENKs produce no lead and `reachable` is never set.

- [ ] **Step 3: Update `_reclassify_all`**

In `src/brreg_leads/ingest.py`, replace the query + snapshot construction in `_reclassify_all`:

```python
def _reclassify_all(
    conn: sqlite3.Connection,
    enk_matches: dict[str, dict],
    today: date,
) -> int:
    cutoff = (today - timedelta(days=NEW_BUSINESS_LOOKBACK_DAYS)).isoformat()
    rows = conn.execute(
        """
        SELECT e.orgnr, e.organisasjonsform, e.hjemmeside, e.naeringskode1_kode,
               e.konkurs, e.under_avvikling, e.slettedato, e.registreringsdato,
               COALESCE(NULLIF(e.epost, ''), x.epost) AS epost
        FROM enheter e
        LEFT JOIN enrichment x ON x.orgnr = e.orgnr
        WHERE e.organisasjonsform = 'AS'
           OR (e.organisasjonsform = 'ENK' AND e.registreringsdato >= ?)
        """,
        (cutoff,),
    ).fetchall()
    count = 0
    for row in rows:
        snap = classify.EnhetSnapshot(
            orgnr=row["orgnr"],
            organisasjonsform=row["organisasjonsform"],
            hjemmeside=row["hjemmeside"],
            naeringskode1_kode=row["naeringskode1_kode"],
            konkurs=bool(row["konkurs"]),
            under_avvikling=bool(row["under_avvikling"]),
            slettedato=row["slettedato"],
            last_oppdatering_dato=_last_oppdatering_dato(conn, row["orgnr"]),
            epost=row["epost"],
            registreringsdato=row["registreringsdato"],
        )
        cohorts, score = classify.classify(
            snap,
            enk_conversion=row["orgnr"] in enk_matches,
            today=today,
        )
        upsert_lead(conn, row["orgnr"], cohorts, score)
        if cohorts:
            count += 1
            if row["orgnr"] in enk_matches:
                match = enk_matches[row["orgnr"]]
                note = (
                    f"ENK→AS heuristic match ({match['match_strength']}): "
                    f"person {match['person_navn']!r} also INNH of ENK {match['enk_orgnr']}"
                )
                conn.execute(
                    "UPDATE leads SET notes = COALESCE(notes, ?) WHERE orgnr = ? AND notes IS NULL",
                    (note, row["orgnr"]),
                )
    return count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ingest.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brreg_leads/ingest.py tests/test_ingest.py
git commit -m "feat: reclassify recent ENKs with email coalesced from brreg/proff"
```

---

## Task 5: Dashboard — reachable-first sort + organisasjonsform in the lead query

**Files:**
- Modify: `src/brreg_leads/web/routes.py:45-72` (`_build_lead_query`)
- Test: `tests/test_routes.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_routes.py`, extend `_seed` to give one lead an email (so reachable ordering is observable), then add a test. Add this enhet+lead inside `_seed` (append to the `enheter` and `leads` lists):

```python
        ("400000004", "DELTA WEB ENK", "ENK", "2026-05-18", "0301", "OSLO", "62.010", "IT", None, 8),
```

and in `leads`:

```python
        ("400000004", ["reachable", "no_website"], 8),
```

Also set its email by adding, just before `conn.commit()` in `_seed`:

```python
    conn.execute("UPDATE enheter SET epost='founder@delta.no' WHERE orgnr='400000004'")
```

Then add the test:

```python
def test_reachable_lead_sorts_first(client):
    r = client.get("/")
    body = r.text
    # DELTA (has email) must appear before GAMMA (no email), regardless of score.
    assert body.index("DELTA WEB ENK") < body.index("GAMMA HOLDING AS")


def test_orgform_shown(client):
    r = client.get("/")
    assert "ENK" in r.text  # the AS/ENK badge for DELTA
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_routes.py::test_reachable_lead_sorts_first -v`
Expected: FAIL — current `ORDER BY l.score DESC` puts GAMMA (3) ahead is fine, but DELTA has score 8 so this specific assert may pass by score; the *intent* is reachability-independent ordering. To make the test meaningful, also confirm `test_orgform_shown` FAILS first (organisasjonsform not yet selected/rendered). Expect `AssertionError` on the badge.

- [ ] **Step 3: Update `_build_lead_query`**

In `src/brreg_leads/web/routes.py`, replace `_build_lead_query`'s SELECT/ORDER BY:

```python
def _build_lead_query(
    cohort: str | None,
    status: str | None,
    kommune: str | None,
    naering_prefix: str | None,
    search: str | None,
    reachable: str | None = None,
    orgform: str | None = None,
    website: str | None = None,
) -> tuple[str, list[Any]]:
    where_sql, params = _build_where(
        cohort, status, kommune, naering_prefix, search, reachable, orgform, website
    )
    sql = f"""
        SELECT l.orgnr, l.cohorts_json, l.score, l.status, l.notes, l.last_contacted_at,
               e.navn, e.organisasjonsform, e.kommunenummer, e.kommune_navn, e.registreringsdato,
               e.naeringskode1_kode, e.naeringskode1_beskrivelse,
               COALESCE(NULLIF(e.epost, ''), x.epost) AS epost,
               COALESCE(NULLIF(e.telefon, ''), x.telefon) AS telefon,
               COALESCE(NULLIF(e.hjemmeside, ''), x.hjemmeside) AS hjemmeside,
               e.antall_ansatte,
               CASE
                 WHEN e.epost IS NOT NULL AND e.epost <> '' THEN 'brreg'
                 WHEN x.source IS NOT NULL THEN x.source
                 ELSE NULL
               END AS contact_source
        FROM leads l
        JOIN enheter e ON e.orgnr = l.orgnr
        LEFT JOIN enrichment x ON x.orgnr = l.orgnr
        WHERE {where_sql}
        ORDER BY (CASE WHEN COALESCE(NULLIF(e.epost, ''), x.epost) IS NOT NULL THEN 1 ELSE 0 END) DESC,
                 l.score DESC, e.registreringsdato DESC
    """
    return sql, params
```

Note: `_build_where` gains three params in Task 6; this task adds them to the call now and Task 6 implements them. To keep this task self-contained and green, also apply the Task 6 `_build_where` signature change in Step 4 below.

- [ ] **Step 4: Extend `_build_where` signature (no new clauses yet)**

In `src/brreg_leads/web/routes.py`, change the `_build_where` signature so the new call compiles. Add the params but leave clause logic for Task 6:

```python
def _build_where(
    cohort: str | None,
    status: str | None,
    kommune: str | None,
    naering_prefix: str | None,
    search: str | None,
    reachable: str | None = None,
    orgform: str | None = None,
    website: str | None = None,
) -> tuple[str, list[Any]]:
    where = ["l.cohorts_json IS NOT NULL"]
    params: list[Any] = []
    if cohort:
        where.append("EXISTS (SELECT 1 FROM json_each(l.cohorts_json) WHERE value = ?)")
        params.append(cohort)
    if status:
        where.append("l.status = ?")
        params.append(status)
    if kommune:
        where.append("e.kommunenummer = ?")
        params.append(kommune)
    if naering_prefix:
        where.append("e.naeringskode1_kode LIKE ?")
        params.append(naering_prefix + "%")
    if search:
        where.append("(e.navn LIKE ? OR e.orgnr LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    return " AND ".join(where), params
```

- [ ] **Step 5: Render the AS/ENK badge**

In `src/brreg_leads/web/templates/leads.html`, add a Type column. In `<thead>` after the Navn `<th>` (line ~68), add:

```html
      <th class="px-3 py-2">Type</th>
```

In `<tbody>` after the Navn `<td>` (line ~82), add:

```html
      <td class="px-3 py-2"><span class="text-xs font-mono px-1.5 py-0.5 rounded bg-slate-100 border border-slate-200">{{ l.organisasjonsform }}</span></td>
```

Update the two `colspan="9"` occurrences to `colspan="10"` (the empty-state row, line ~101).

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_routes.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/brreg_leads/web/routes.py src/brreg_leads/web/templates/leads.html tests/test_routes.py
git commit -m "feat: reachable-first lead sort and AS/ENK badge"
```

---

## Task 6: Dashboard filters — reachable / orgform / website

**Files:**
- Modify: `src/brreg_leads/web/routes.py` (`_build_where` clauses, `_build_count_query` join, `index`, `export_csv`)
- Modify: `src/brreg_leads/web/templates/leads.html` (filter controls + export/show-all links)
- Test: `tests/test_routes.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_routes.py`:

```python
def test_filter_reachable_only(client):
    r = client.get("/?reachable=1")
    body = r.text
    assert "DELTA WEB ENK" in body          # has email
    assert "GAMMA HOLDING AS" not in body    # no email


def test_filter_orgform_enk(client):
    r = client.get("/?orgform=ENK")
    body = r.text
    assert "DELTA WEB ENK" in body
    assert "ACME RETAIL AS" not in body


def test_filter_website_none(client):
    r = client.get("/?website=none")
    body = r.text
    assert "ACME RETAIL AS" in body          # no website
    assert "BETA CONSULTING AS" not in body  # has website
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_routes.py -k "filter_reachable or filter_orgform or filter_website" -v`
Expected: FAIL — the params are accepted by `index` only after Step 4, and `_build_where` has no clauses for them yet, so filtering doesn't happen.

- [ ] **Step 3: Add the WHERE clauses + count-query join**

In `src/brreg_leads/web/routes.py`, append to `_build_where` (before the `return`):

```python
    if reachable:
        where.append("COALESCE(NULLIF(e.epost, ''), x.epost) IS NOT NULL")
    if orgform:
        where.append("e.organisasjonsform = ?")
        params.append(orgform)
    if website == "has":
        where.append("COALESCE(NULLIF(e.hjemmeside, ''), x.hjemmeside) IS NOT NULL")
    elif website == "none":
        where.append("COALESCE(NULLIF(e.hjemmeside, ''), x.hjemmeside) IS NULL")
```

Because these clauses reference the `enrichment` alias `x`, add the join to `_build_count_query`:

```python
def _build_count_query(
    cohort: str | None,
    status: str | None,
    kommune: str | None,
    naering_prefix: str | None,
    search: str | None,
    reachable: str | None = None,
    orgform: str | None = None,
    website: str | None = None,
) -> tuple[str, list[Any]]:
    where_sql, params = _build_where(
        cohort, status, kommune, naering_prefix, search, reachable, orgform, website
    )
    return (
        "SELECT COUNT(*) AS c FROM leads l "
        "JOIN enheter e ON e.orgnr = l.orgnr "
        "LEFT JOIN enrichment x ON x.orgnr = l.orgnr "
        f"WHERE {where_sql}",
        params,
    )
```

- [ ] **Step 4: Thread the params through `index` and `export_csv`**

In `src/brreg_leads/web/routes.py`, update `index`'s signature and calls:

```python
@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    cohort: str | None = None,
    status: str | None = None,
    kommune: str | None = None,
    naering_prefix: str | None = None,
    search: str | None = None,
    reachable: str | None = None,
    orgform: str | None = None,
    website: str | None = None,
    limit: int = 2000,
):
    sql, params = _build_lead_query(
        cohort, status, kommune, naering_prefix, search, reachable, orgform, website
    )
    sql += " LIMIT ?"
    params.append(limit)
    count_sql, count_params = _build_count_query(
        cohort, status, kommune, naering_prefix, search, reachable, orgform, website
    )
```

Add the three to the `filters` dict in the template context:

```python
            "filters": {
                "cohort": cohort or "",
                "status": status or "",
                "kommune": kommune or "",
                "naering_prefix": naering_prefix or "",
                "search": search or "",
                "reachable": reachable or "",
                "orgform": orgform or "",
                "website": website or "",
                "limit": limit,
            },
```

Update `export_csv`'s signature and `_build_lead_query` call the same way:

```python
@router.get("/export.csv")
def export_csv(
    cohort: str | None = None,
    status: str | None = None,
    kommune: str | None = None,
    naering_prefix: str | None = None,
    search: str | None = None,
    reachable: str | None = None,
    orgform: str | None = None,
    website: str | None = None,
):
    sql, params = _build_lead_query(
        cohort, status, kommune, naering_prefix, search, reachable, orgform, website
    )
```

- [ ] **Step 5: Add filter controls to the template**

In `src/brreg_leads/web/templates/leads.html`, inside the `<form method="get">` (after the Search label, ~line 41), add:

```html
    <label class="text-xs text-slate-600">Type
      <select name="orgform" class="block border rounded px-2 py-1 text-sm">
        <option value="">All</option>
        <option value="AS" {% if filters.orgform == 'AS' %}selected{% endif %}>AS</option>
        <option value="ENK" {% if filters.orgform == 'ENK' %}selected{% endif %}>ENK</option>
      </select>
    </label>
    <label class="text-xs text-slate-600">Website
      <select name="website" class="block border rounded px-2 py-1 text-sm">
        <option value="">All</option>
        <option value="none" {% if filters.website == 'none' %}selected{% endif %}>No website</option>
        <option value="has" {% if filters.website == 'has' %}selected{% endif %}>Has website</option>
      </select>
    </label>
    <label class="text-xs text-slate-600 flex items-center gap-1 mt-4">
      <input type="checkbox" name="reachable" value="1" {% if filters.reachable %}checked{% endif %}> Reachable only
    </label>
```

Update the **Export CSV** link (~line 46) and the **Show all** link (~line 60) to carry the new params. Replace the Export CSV `href` with:

```html
    <a href="/export.csv?cohort={{ filters.cohort }}&status={{ filters.status }}&kommune={{ filters.kommune }}&naering_prefix={{ filters.naering_prefix }}&search={{ filters.search }}&reachable={{ filters.reachable }}&orgform={{ filters.orgform }}&website={{ filters.website }}"
       class="text-sm text-blue-700 underline ml-auto">Export CSV</a>
```

Replace the Show-all `href` with:

```html
    <a href="?cohort={{ filters.cohort }}&status={{ filters.status }}&kommune={{ filters.kommune }}&naering_prefix={{ filters.naering_prefix }}&search={{ filters.search }}&reachable={{ filters.reachable }}&orgform={{ filters.orgform }}&website={{ filters.website }}&limit={{ filtered_total }}"
       class="text-blue-700 underline">Show all {{ filtered_total }}</a>
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_routes.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/brreg_leads/web/routes.py src/brreg_leads/web/templates/leads.html tests/test_routes.py
git commit -m "feat: reachable/orgform/website dashboard filters"
```

---

## Task 7: Today view — coalesce enrichment email

**Files:**
- Modify: `src/brreg_leads/web/routes.py:154-160` (`today` `base_select`)
- Test: `tests/test_routes.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_routes.py` (DELTA from Task 5 has a Brreg email already; to test the enrichment path, add a proff-only-email enhet in `_seed`). Append to `_seed`'s `enheter` and `leads`:

```python
        ("500000005", "EPSILON CAFE ENK", "ENK", "2026-05-19", "0301", "OSLO", "56.101", "Kafe", None, 8),
```
```python
        ("500000005", ["reachable", "no_website"], 8),
```
and before `conn.commit()`:

```python
    conn.execute(
        "INSERT INTO enrichment (orgnr, epost, source, fetched_at) VALUES ('500000005','cafe@proff.no','proff','2026-05-23')"
    )
```

Test:

```python
def test_today_shows_enrichment_email(client):
    r = client.get("/today")
    assert "cafe@proff.no" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_routes.py::test_today_shows_enrichment_email -v`
Expected: FAIL — `today`'s `base_select` reads `e.epost` only, which is NULL for EPSILON.

- [ ] **Step 3: Update the `today` base query**

In `src/brreg_leads/web/routes.py`, change `today`'s `base_select`:

```python
    base_select = """
        SELECT l.orgnr, l.cohorts_json, l.score, l.status, l.last_contacted_at,
               e.navn, e.organisasjonsform, e.kommune_navn, e.kommunenummer, e.registreringsdato,
               e.naeringskode1_kode, e.naeringskode1_beskrivelse,
               COALESCE(NULLIF(e.epost, ''), x.epost) AS epost,
               COALESCE(NULLIF(e.telefon, ''), x.telefon) AS telefon,
               COALESCE(NULLIF(e.hjemmeside, ''), x.hjemmeside) AS hjemmeside
        FROM leads l
        JOIN enheter e ON e.orgnr = l.orgnr
        LEFT JOIN enrichment x ON x.orgnr = l.orgnr
    """
```

The `WHERE`/`ORDER BY` clauses appended later use `l.` and `e.` columns only, so they remain valid.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/brreg_leads/web/routes.py tests/test_routes.py
git commit -m "feat: coalesce proff email into Today view"
```

---

## Task 8: Full test sweep + docs

**Files:**
- Modify: `CLAUDE.md`, `README.md`

- [ ] **Step 1: Run the entire suite**

Run: `pytest`
Expected: all green. If anything fails, fix before continuing.

- [ ] **Step 2: Update `CLAUDE.md`**

Make these edits in `CLAUDE.md`:

- "What this project is" — change "newly-registered AS companies" to "newly-registered **AS and ENK** companies", and note the lead gate is reachability (has email on Brreg or proff).
- Cohorts table — add a row:
  `| reachable | has an email (Brreg epostadresse or proff enrichment); primary gate + sort key |`
  and note that the dashboard sorts reachable leads first.
- "Daily ingest flow" — add a step: after AS paging, page `/enheter?organisasjonsform=ENK&…fraRegistreringsdato…=since` and upsert; proff-enrich in-window ENKs missing a Brreg email; reclassify covers ASes plus ENKs registered within `NEW_BUSINESS_LOOKBACK_DAYS`.
- "ENK seeding" section — clarify there are now two ENK roles: (a) seeded *active* ENKs for conversion detection (existing) and (b) *newly-registered* ENKs that become reachable leads (new), gated by `NEW_BUSINESS_LOOKBACK_DAYS`.
- "Out of scope" — leave the email-*sending* item; the leads change does not touch it.

- [ ] **Step 3: Update `README.md`**

Reflect that the tool targets reachable AS **and** ENK, that the list is sorted reachable-first, and mention the reachable/type/website filters.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: reachable-first leads and ENK targeting"
```

---

## Self-review notes (resolved)

- **Spec coverage:** reachable cohort (T1), ENK acceptance + recency guard (T1), ENK ingest (T3), ENK proff-enrichment from Brreg-or-proff (T3), reclassify scope + email coalesce (T4), reachable-first sort (T5), AS/ENK badge (T5), filters (T6), Today coalesce (T7), tests (each task), docs (T8). All spec sections map to a task.
- **Type consistency:** `iter_new_enheter(organisasjonsform, kommunenummer, registered_from, ...)` used identically in client + ingest. `EnhetSnapshot` gains `epost`/`registreringsdato` (defaulted) and every constructor call that needs them passes them (only `_reclassify_all`; test helper `_snap` relies on defaults). `_build_where`/`_build_count_query`/`_build_lead_query` all share the same `(…, reachable, orgform, website)` tail.
- **Enrichment skip:** no change to `enrich.enrich_orgnrs` — recent ENKs missing email are pre-filtered and passed with `skip_if_brreg_has_email=False`.
- **colspan:** leads.html empty-state row updated 9 → 10 after adding the Type column.
```
