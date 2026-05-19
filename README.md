# brreg-platform

Lead-generation tool for a Norwegian web/email agency. Pulls newly-registered
ASes from the Brønnøysundregistrene open Enhetsregisteret API, tags them by
cohort (no website, target industry, ENK→AS conversion, recently moved), and
exposes a local dashboard for working leads.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## First run

```powershell
python -m brreg_leads backfill --since 2026-04-19   # new ASes for last 30 days
python -m brreg_leads seed-enk                       # one-shot, enables ENK→AS detection
```

`backfill` populates `data/leads.db` with new ASes in Oslo + Akershus
(see `src/brreg_leads/config.py` to edit the kommune list).

`seed-enk` populates currently-active ENKs in the same kommuner. This is
required for ENK→AS conversion detection — without it, the matcher has
nothing to anchor against. Roles for those ENKs are fetched **lazily**, only
once an ENK appears as deleted in the daily `/oppdateringer` feed. Expect the
seed to take ~9 minutes for all 10 kommuner.

## Enrichment

```powershell
python -m brreg_leads enrich          # fill in contact info for leads we don't have it for yet
python -m brreg_leads enrich --all    # re-enrich every lead
```

Brreg leaves `epost`/`telefon` empty for ~80% of new ASes. The enrichment
table + dashboard plumbing is in place to surface contact info from a
secondary source, with a "via <source>" badge when the value is non-Brreg.

**Current source — proff.no — is unreliable.** proff.no is behind an AWS WAF
JavaScript challenge, so a plain HTTP GET returns a challenge page instead of
the company data. The parser correctly returns `source="none"` for these, so
enrichment is currently a safe no-op — no false data, but also no hits.

The schema, dashboard rendering, and CLI work for any source that returns
plain HTML. To make enrichment actually populate, plug in one of:

- **Hunter.io API** (50 free lookups/month, domain → email) — would need a
  `HunterClient` and a `hunter` source label.
- **Google Places API** (generous free tier, name+address → phone/website).
- **Headless browser** for proff.no specifically (Playwright) — heavy but
  works.

The `enrichment` table already has a `source` column so multiple providers
can coexist if you ever add more.

## Daily run

```powershell
python -m brreg_leads ingest
```

Uses the stored cursor and pulls only what's new since the last run.

## Dashboard

```powershell
python -m brreg_leads serve
```

Then open <http://localhost:8000>.

- Filter by cohort (no_website / target_industry / enk_conversion / recently_moved)
- Filter by kommune, næringskode prefix, search by name/orgnr
- Update lead status (new / contacted / interested / won / lost / ignored) with notes
- Export filtered view as CSV

## Scheduling on Windows

Open Task Scheduler → Create Basic Task:

- Trigger: Daily, 07:00
- Action: Start a program → `C:\Users\andym\CodeProjects\brreg-platform\run-ingest.bat`

Logs to `data/ingest.log`.

## Tests

```powershell
pytest
```

Unit tests cover the cohort classifier and the ENK→AS matcher logic. They run
fully offline (no network).

## Configuration

Edit `src/brreg_leads/config.py`:

- `KOMMUNER` — kommunenummer codes you want to monitor.
- `NAERINGSKODE_WHITELIST_PREFIXES` — næringskode prefixes that count as
  "target industry" for your agency.
- `COHORT_WEIGHTS` — change how cohort scores combine (higher score = ranked
  higher in dashboard).

## Data sources

- Brreg Enhetsregisteret API: <https://data.brreg.no/enhetsregisteret/api/docs/index.html>
- License: NLOD (Norsk lisens for offentlige data)
- No auth required, no documented rate limits. The client throttles to ~2 req/s
  to be a good citizen.

## Limitations

- ENK→AS conversion is a heuristic (name + kommune match against recently-deleted
  ENKs). Expect some false negatives if names changed; false positives possible
  for common names. The matched ENK orgnr is shown in lead notes — verify before
  acting.
- ENK→AS detection is **forward-only from the moment you run `seed-enk`**.
  The Brreg public API doesn't let you query "what was deleted last month?",
  so conversions where the ENK was deleted before the seed cannot be
  recovered. Since ENK→AS transitions usually take 1–3 weeks from old close
  to new register, the cold-start window is short.
- Email and phone fields in Brreg are frequently empty. Address + næringskode
  are reliable.
- Outbound communication (sending offers) is intentionally not part of this
  tool — copy contact details into your own mail client, keeping you as the
  responsible data controller under GDPR.
