"""Enrichment tests: HTML parser + DB upsert flow."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from brreg_leads import enrich
from brreg_leads.db import SCHEMA

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def test_parses_phone_email_and_website_from_proff():
    result = enrich.parse_proff_html(_read("proff_sample.html"))
    assert result.telefon == "+47 22 12 34 56"
    assert result.epost == "post@acmeretail.no"
    assert result.hjemmeside == "https://www.acmeretail.no/"
    assert result.source == "proff"


def test_returns_empty_when_no_contact_info():
    result = enrich.parse_proff_html(_read("proff_empty.html"))
    assert result.telefon is None
    assert result.epost is None
    assert result.hjemmeside is None
    assert result.source == "none"


def test_parser_ignores_proff_internal_links():
    result = enrich.parse_proff_html(_read("proff_sample.html"))
    # Even though there's an editor@proff.no mailto and a proff.no website link,
    # we should pick the entity's own contact info first.
    assert "proff.no" not in (result.epost or "")
    assert "proff.no" not in (result.hjemmeside or "")


def test_parser_handles_empty_html():
    assert enrich.parse_proff_html("").source == "none"


def test_parser_returns_none_for_waf_challenge_page():
    """proff.no is currently behind AWS WAF; the challenge page contains no
    real contact info. We must not confuse it with a successful fetch."""
    result = enrich.parse_proff_html(_read("proff_waf.html"))
    assert result.source == "none"
    assert result.epost is None
    assert result.telefon is None


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    yield c
    c.close()


class FakeProffClient:
    def __init__(self, by_orgnr: dict[str, str]):
        self._pages = by_orgnr
        self.calls: list[str] = []

    def fetch(self, orgnr: str) -> str | None:
        self.calls.append(orgnr)
        return self._pages.get(orgnr)


def test_enrich_orgnrs_upserts_into_enrichment_table(conn):
    conn.execute(
        "INSERT INTO enheter (orgnr, navn, organisasjonsform, raw_json, first_seen_at, last_refreshed_at)"
        " VALUES ('100000001', 'Acme', 'AS', '{}', 'now', 'now')"
    )
    proff = FakeProffClient({"100000001": _read("proff_sample.html")})
    summary = enrich.enrich_orgnrs(conn, ["100000001"], proff=proff)
    assert summary == {"attempted": 1, "enriched": 1, "skipped": 0}
    row = conn.execute(
        "SELECT epost, telefon, hjemmeside, source FROM enrichment WHERE orgnr = ?",
        ("100000001",),
    ).fetchone()
    assert row["epost"] == "post@acmeretail.no"
    assert row["telefon"] == "+47 22 12 34 56"
    assert row["hjemmeside"] == "https://www.acmeretail.no/"
    assert row["source"] == "proff"


def test_enrich_skips_when_brreg_already_complete(conn):
    conn.execute(
        "INSERT INTO enheter (orgnr, navn, organisasjonsform, epost, telefon, raw_json, first_seen_at, last_refreshed_at)"
        " VALUES ('100000001', 'Acme', 'AS', 'a@b.no', '12345678', '{}', 'now', 'now')"
    )
    proff = FakeProffClient({})
    summary = enrich.enrich_orgnrs(conn, ["100000001"], proff=proff)
    assert summary["skipped"] == 1
    assert summary["attempted"] == 0
    assert proff.calls == []


def test_enrich_handles_orgnr_with_no_proff_page(conn):
    conn.execute(
        "INSERT INTO enheter (orgnr, navn, organisasjonsform, raw_json, first_seen_at, last_refreshed_at)"
        " VALUES ('999999999', 'Unknown', 'AS', '{}', 'now', 'now')"
    )
    proff = FakeProffClient({})  # returns None for unknown orgnrs
    summary = enrich.enrich_orgnrs(conn, ["999999999"], proff=proff)
    assert summary == {"attempted": 1, "enriched": 0, "skipped": 0}
    row = conn.execute("SELECT * FROM enrichment WHERE orgnr = ?", ("999999999",)).fetchone()
    assert row is None
