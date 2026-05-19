import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS enheter (
    orgnr                     TEXT PRIMARY KEY,
    navn                      TEXT NOT NULL,
    organisasjonsform         TEXT NOT NULL,
    registreringsdato         TEXT,
    slettedato                TEXT,
    kommunenummer             TEXT,
    kommune_navn              TEXT,
    forretningsadresse_json   TEXT,
    postadresse_json          TEXT,
    naeringskode1_kode        TEXT,
    naeringskode1_beskrivelse TEXT,
    epost                     TEXT,
    telefon                   TEXT,
    mobil                     TEXT,
    hjemmeside                TEXT,
    antall_ansatte            INTEGER,
    konkurs                   INTEGER NOT NULL DEFAULT 0,
    under_avvikling           INTEGER NOT NULL DEFAULT 0,
    raw_json                  TEXT NOT NULL,
    first_seen_at             TEXT NOT NULL,
    last_refreshed_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_enheter_orgform_regdato
    ON enheter(organisasjonsform, registreringsdato);
CREATE INDEX IF NOT EXISTS idx_enheter_kommune
    ON enheter(kommunenummer);
CREATE INDEX IF NOT EXISTS idx_enheter_slettedato
    ON enheter(slettedato);

CREATE TABLE IF NOT EXISTS roller (
    orgnr        TEXT NOT NULL,
    rolle_type   TEXT NOT NULL,
    person_navn  TEXT NOT NULL,
    fra_dato     TEXT,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (orgnr, rolle_type, person_navn)
);
CREATE INDEX IF NOT EXISTS idx_roller_person ON roller(person_navn);

CREATE TABLE IF NOT EXISTS oppdateringer (
    oppdateringsid INTEGER PRIMARY KEY,
    orgnr          TEXT NOT NULL,
    endringstype   TEXT,
    dato           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oppdateringer_orgnr_dato
    ON oppdateringer(orgnr, dato);

CREATE TABLE IF NOT EXISTS leads (
    orgnr             TEXT PRIMARY KEY REFERENCES enheter(orgnr),
    cohorts_json      TEXT NOT NULL,
    score             INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'new',
    notes             TEXT,
    last_contacted_at TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score);

CREATE TABLE IF NOT EXISTS ingest_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrichment (
    orgnr       TEXT PRIMARY KEY,
    epost       TEXT,
    telefon     TEXT,
    hjemmeside  TEXT,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lead_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    orgnr       TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lead_notes_orgnr_created
    ON lead_notes(orgnr, created_at DESC);
"""


def _migrate_legacy_notes(conn: sqlite3.Connection) -> None:
    """One-shot: pull any non-null leads.notes into a lead_notes row, then null it out."""
    rows = conn.execute(
        "SELECT orgnr, notes, updated_at FROM leads WHERE notes IS NOT NULL AND notes <> ''"
    ).fetchall()
    for row in rows:
        already = conn.execute(
            "SELECT 1 FROM lead_notes WHERE orgnr = ? LIMIT 1", (row["orgnr"],)
        ).fetchone()
        if already:
            continue
        conn.execute(
            "INSERT INTO lead_notes (orgnr, body, created_at) VALUES (?, ?, ?)",
            (row["orgnr"], row["notes"], row["updated_at"]),
        )
    conn.execute("UPDATE leads SET notes = NULL WHERE notes IS NOT NULL")


def init_db(path: Path | None = None) -> None:
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        _migrate_legacy_notes(conn)
        conn.commit()


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    target = path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM ingest_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO ingest_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
