"""FastAPI web layer tests using TestClient against a temp SQLite DB."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from brreg_leads import config, db
from brreg_leads.db import SCHEMA


def _seed(db_path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    enheter = [
        ("100000001", "ACME RETAIL AS", "AS", "2026-05-10", "0301", "OSLO", "47.110", "Detaljhandel", None, 4),
        ("200000002", "BETA CONSULTING AS", "AS", "2026-05-12", "0301", "OSLO", "70.200", "Konsulent", "betaconsult.no", 3),
        ("300000003", "GAMMA HOLDING AS", "AS", "2026-05-15", "0301", "OSLO", "64.200", "Holding", None, 0),
    ]
    enheter.append(
        ("400000004", "DELTA WEB ENK", "ENK", "2026-05-18", "0301", "OSLO", "62.010", "IT", None, 8),
    )
    for orgnr, navn, form, regdato, knr, knavn, naering, beskr, hjemme, score in enheter:
        conn.execute(
            """
            INSERT INTO enheter (orgnr, navn, organisasjonsform, registreringsdato,
                                 kommunenummer, kommune_navn, naeringskode1_kode,
                                 naeringskode1_beskrivelse, hjemmeside,
                                 raw_json, first_seen_at, last_refreshed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
            """,
            (orgnr, navn, form, regdato, knr, knavn, naering, beskr, hjemme, now, now),
        )
    leads = [
        ("100000001", ["no_website", "target_industry"], 5),
        ("200000002", ["target_industry"], 2),
        ("300000003", ["no_website"], 3),
        ("400000004", ["reachable", "no_website"], 2),
    ]
    for orgnr, cohorts, score in leads:
        conn.execute(
            """INSERT INTO leads (orgnr, cohorts_json, score, status, created_at, updated_at)
               VALUES (?, ?, ?, 'new', ?, ?)""",
            (orgnr, json.dumps(cohorts), score, now, now),
        )
    conn.execute("UPDATE enheter SET epost='founder@delta.no' WHERE orgnr='400000004'")
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    _seed(db_path)
    from brreg_leads.web.app import create_app
    return TestClient(create_app())


def test_index_renders_all_leads(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "ACME RETAIL AS" in body
    assert "BETA CONSULTING AS" in body
    assert "GAMMA HOLDING AS" in body
    assert "DELTA WEB ENK" in body
    assert "Showing" in body
    assert "<span class=\"font-semibold\">4</span>" in body


def test_index_cohort_filter(client):
    r = client.get("/?cohort=target_industry")
    assert r.status_code == 200
    assert "ACME RETAIL AS" in r.text
    assert "BETA CONSULTING AS" in r.text
    assert "GAMMA HOLDING AS" not in r.text


def test_index_limit_shows_truncation_banner(client):
    r = client.get("/?limit=1")
    assert r.status_code == 200
    assert "Show all 4" in r.text


def test_lead_detail_renders(client):
    r = client.get("/lead/100000001")
    assert r.status_code == 200
    assert "ACME RETAIL AS" in r.text
    assert "47.110" in r.text


def test_lead_detail_404_for_unknown(client):
    r = client.get("/lead/999999999")
    assert r.status_code == 404


def test_status_update_persists(client, tmp_path):
    r = client.post(
        "/lead/100000001/status",
        data={"status": "contacted", "notes": "called Mon"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    detail = client.get("/lead/100000001")
    assert "called Mon" in detail.text
    assert "Status: new → contacted" in detail.text
    db_path = tmp_path / "test.db"
    row = sqlite3.connect(db_path).execute(
        "SELECT status, last_contacted_at FROM leads WHERE orgnr = ?",
        ("100000001",),
    ).fetchone()
    assert row[0] == "contacted"
    assert row[1] is not None
    notes = sqlite3.connect(db_path).execute(
        "SELECT body FROM lead_notes WHERE orgnr = ? ORDER BY id",
        ("100000001",),
    ).fetchall()
    bodies = [n[0] for n in notes]
    assert "Status: new → contacted" in bodies
    assert "called Mon" in bodies


def test_add_note_endpoint(client, tmp_path):
    r = client.post("/lead/100000001/note", data={"body": "found their email"}, follow_redirects=False)
    assert r.status_code == 303
    db_path = tmp_path / "test.db"
    notes = sqlite3.connect(db_path).execute(
        "SELECT body FROM lead_notes WHERE orgnr = ?", ("100000001",)
    ).fetchall()
    assert ("found their email",) in notes


def test_today_view(client):
    r = client.get("/today")
    assert r.status_code == 200
    body = r.text
    # ACME RETAIL has score=5, status=new, registered today → call list
    assert "Call list" in body
    assert "ACME RETAIL AS" in body
    # No followups or interested yet
    assert "Follow up" not in body
    assert "Open opportunities" not in body


def test_status_update_rejects_invalid(client):
    r = client.post(
        "/lead/100000001/status",
        data={"status": "bogus", "notes": ""},
    )
    assert r.status_code == 400


def test_export_csv(client):
    r = client.get("/export.csv?cohort=target_industry")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().split("\n")
    assert lines[0].startswith("orgnr,navn,")
    assert len(lines) == 3  # header + 2 target_industry leads
    assert "ACME RETAIL AS" in r.text
    assert "BETA CONSULTING AS" in r.text


def test_empty_db(tmp_path, monkeypatch):
    db_path = tmp_path / "empty.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(db, "DB_PATH", db_path)
    # Don't seed
    from brreg_leads.web.app import create_app
    c = TestClient(create_app())
    r = c.get("/")
    assert r.status_code == 200
    assert "No leads" in r.text


def test_reachable_lead_sorts_first(client):
    r = client.get("/")
    body = r.text
    # DELTA (score=2, has email) sorts above GAMMA (score=3, no email) only because email-tier beats score.
    assert body.index("DELTA WEB ENK") < body.index("GAMMA HOLDING AS")


def test_orgform_shown(client):
    r = client.get("/")
    body = r.text
    assert ">ENK<" in body  # ENK badge for DELTA
    assert ">AS<" in body   # AS badge for the AS leads


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
