# Reachable-first leads (AS + ENK) — design

**Date:** 2026-05-23
**Status:** Approved, ready for implementation plan
**Scope:** Leads/classification + ingest + dashboard. Email outreach is a *separate*
follow-up spec and is explicitly **not** part of this work.

## Problem

Newly-registered companies with no email are effectively unreachable — the operator
confirmed manually that for these there is no email/website findable anywhere (Google,
proff, Brreg). Chasing contact info for them is wasted effort.

The valuable leads are the ones that are **reachable** — they already have an email in
Brreg (~20% of new ASes) or in our proff `enrichment` table. Today the tool:

- only treats a company as a lead if it matches a *relevance* cohort
  (`no_website`, `target_industry`, `enk_conversion`, `recently_moved`), and
- hard-rejects anything that isn't an `AS` in `classify`.

So reachable companies that don't match a relevance cohort are invisible, the
has-website upsell segment never appears, and sole proprietorships (ENK) — a core
web-agency audience — are excluded entirely.

## Goal

Make **reachability the primary signal**. A newly-registered **AS or ENK with an
email** is a lead, pinned to the top of the list. Website status decides the *pitch*,
not whether it qualifies:

- **Reachable + no website** → prime "build a site" lead.
- **Reachable + has website** → "upsell features" lead.
- **No email** → kept in the DB and list but sorted to the bottom (unreachable).

## Non-goals

- Email enrichment for no-email companies (confirmed not findable; not worth building).
- Sending email / outreach automation — that is the next, separate spec.
- Fetching roles or proff-enriching new ENKs (we only want ENKs that *already* have an
  email, so there is nothing to enrich; skipping keeps API volume sane).
- Backfill-scale ENK partitioning (documented limitation, see Ingest).

## Design

### 1. Classification (`classify.py`, `config.py`)

`EnhetSnapshot` gains two fields it doesn't carry today:
- `epost: str | None` — already-known email (Brreg or enrichment; coalesced by caller).
- `registreringsdato: str | None` — ISO registration date (for the ENK recency guard).

`classify(...)` changes:

1. Accept `organisasjonsform in {"AS", "ENK"}`; still reject everything else and still
   reject `konkurs` / `under_avvikling` / `slettedato`.
2. **ENK recency guard (critical):** if `organisasjonsform == "ENK"` and the company was
   *not* registered within `NEW_BUSINESS_LOOKBACK_DAYS` of `today`, return `[], 0`. This
   prevents the ~30–80k historical active ENKs that `seed-enk` loaded into `enheter`
   (for conversion detection) from becoming leads via `no_website`/`target_industry`.
   AS is **not** recency-gated (preserves `recently_moved` behavior and intentional
   backfill of older ASes).
3. **New `reachable` cohort:** added when `epost` is non-empty.

`config.py`:
- `COHORT_WEIGHTS["reachable"] = 5` (dominant; ranks reachable leads above relevance-only
  ones within the reachable group).
- `NEW_BUSINESS_LOOKBACK_DAYS = 90`.

Cohort/weight table after the change:

| Cohort            | Weight | Qualifies a lead? |
| ----------------- | ------ | ----------------- |
| `reachable`       | 5      | yes (new gate)    |
| `enk_conversion`  | 4      | yes               |
| `no_website`      | 3      | yes               |
| `target_industry` | 2      | yes               |
| `recently_moved`  | 1      | yes               |

Reachability does **not** replace the existing gates — it's additive. A no-email
company that matches a relevance cohort is still a lead; it just sorts below all
reachable ones (see Dashboard sort).

### 2. Reclassification (`ingest._reclassify_all`)

- The per-row query that feeds `classify` must `LEFT JOIN enrichment` and pass
  `COALESCE(NULLIF(e.epost,''), x.epost)` as the snapshot's `epost`, plus
  `e.registreringsdato`.
- **Scope the reclassify query** to `organisasjonsform = 'AS' OR (organisasjonsform =
  'ENK' AND registreringsdato >= <today - NEW_BUSINESS_LOOKBACK_DAYS>)` so we don't
  evaluate + churn `DELETE` against tens of thousands of historical seeded ENKs every run.

### 3. Ingest new ENKs (`brreg_client.py`, `ingest.py`)

- Generalize `iter_new_as(kommune, registered_from, ...)` → it currently hardcodes
  `organisasjonsform: "AS"`. Add an `organisasjonsform` parameter (default `"AS"` to keep
  existing callers working), or introduce `iter_new_enheter`. Either way, one method that
  can page new ASes and new ENKs.
- In `run_ingest`, after `_pull_new_as`, also pull **new ENKs** for each kommune using the
  **same `LAST_POLL_KEY` date cursor** (it's `fraRegistreringsdatoEnhetsregisteret`-based,
  so one cursor serves both forms). Upsert into `enheter` exactly like ASes.
- New ENKs do **not** get role fetching or proff enrichment (non-goal above). The new-AS
  role-fetch + enrichment steps stay AS-only and unchanged.
- The existing `/oppdateringer` refresh + deleted-ENK role hook + `seed-enk` flow are
  untouched.

**Documented limitation:** a one-time backfill with a far-back `--since` could exceed
Brreg's 10 000-offset cap for ENKs in a large kommune. Daily deltas are tiny and
unaffected. We document this rather than build date-partitioning for new-ENK paging now.

### 4. Dashboard (`web/routes.py`, templates)

- **Primary sort key — reachability first.** `_build_lead_query` `ORDER BY` becomes
  `has_email DESC, l.score DESC, e.registreringsdato DESC`, where `has_email` is
  `CASE WHEN COALESCE(NULLIF(e.epost,''), x.epost) IS NOT NULL THEN 1 ELSE 0 END`.
  Reachable leads are always above unreachable ones regardless of score.
- **Surface `organisasjonsform`** in the lead query so the list/detail can show an
  **AS / ENK badge**. Email is already coalesced + shown.
- **New filters** in `_build_where` (and the leads.html filter bar):
  - `reachable` (only leads with an email),
  - `orgform` (`AS` / `ENK`),
  - `website` (`has` / `none`) — lets the operator isolate e.g. *reachable ENK + no
    website* in one click.
  - The `reachable` cohort also appears automatically in the existing cohort filter
    (it's derived from `COHORT_WEIGHTS`).
- CSV export already includes email and inherits the new sort; no change beyond the
  shared query.
- `today.html` query uses `e.epost` directly — coalesce enrichment there too for
  consistency (minor).

### 5. Tests

- `tests/test_classify.py`: ENK accepted when recent; ENK rejected when older than the
  window; `reachable` cohort added on email present/absent; AS still ungated.
- `tests/test_ingest.py`: new-ENK paging upserts ENKs; ENK rows are not role-fetched /
  enriched; reclassify scope excludes old ENKs.
- `tests/test_routes.py`: reachable-first ordering; `reachable` / `orgform` / `website`
  filters; AS/ENK badge data present.

### 6. Docs

- `CLAUDE.md`: update "What this project is" (AS **and** ENK), the cohort table (add
  `reachable`, note it's the primary gate + sort key), the daily-ingest flow (ENK paging),
  and the ENK section (distinguish lead-ENKs from conversion-seed ENKs). Remove/loosen the
  AS-only framing.
- `README`: reflect AS+ENK targeting and the reachable-first list.

## Risks

- **ENK volume explosion** — mitigated by the recency guard (§1.2) and scoped reclassify
  (§2). This is the highest-risk part; tests must cover it explicitly.
- **Backfill offset cap for ENKs** — documented limitation, not handled in v1.
- **Lead-list size growth** — more leads overall; the reachable-first sort + filters keep
  the working set focused.
