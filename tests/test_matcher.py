import sqlite3
from datetime import date

import pytest

from brreg_leads.db import SCHEMA
from brreg_leads.matcher import find_enk_conversions, normalize_name


def test_normalize_name_folds_comma_form():
    assert normalize_name("Hansen, Ola") == "ola hansen"


def test_normalize_name_strips_diacritics_and_case():
    assert normalize_name("Sørensen, Åse") == "ase sorensen"


def test_normalize_name_handles_none():
    assert normalize_name(None) == ""


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    yield c
    c.close()


def _insert_enhet(conn, orgnr, form, regdato=None, slettedato=None, kommune="0301"):
    conn.execute(
        """
        INSERT INTO enheter (orgnr, navn, organisasjonsform, registreringsdato, slettedato,
                             kommunenummer, raw_json, first_seen_at, last_refreshed_at)
        VALUES (?, ?, ?, ?, ?, ?, '{}', '2026-05-19', '2026-05-19')
        """,
        (orgnr, f"Test {orgnr}", form, regdato, slettedato, kommune),
    )


def _insert_rolle(conn, orgnr, rolle_type, person_navn):
    conn.execute(
        "INSERT INTO roller (orgnr, rolle_type, person_navn, fetched_at) VALUES (?, ?, ?, '2026-05-19')",
        (orgnr, rolle_type, person_navn),
    )


def test_matches_when_innh_becomes_dagl_in_same_kommune(conn):
    _insert_enhet(conn, "100000001", "ENK", regdato="2020-01-01", slettedato="2026-04-15", kommune="0301")
    _insert_rolle(conn, "100000001", "INNH", "Hansen, Ola")
    _insert_enhet(conn, "200000002", "AS", regdato="2026-05-01", kommune="0301")
    _insert_rolle(conn, "200000002", "DAGL", "Hansen, Ola")

    matches = find_enk_conversions(conn, today=date(2026, 5, 19))
    assert "200000002" in matches
    assert matches["200000002"]["enk_orgnr"] == "100000001"
    assert matches["200000002"]["match_strength"] == "name+kommune"


def test_falls_back_to_name_only_if_kommune_differs(conn):
    _insert_enhet(conn, "100000001", "ENK", slettedato="2026-04-15", kommune="0301")
    _insert_rolle(conn, "100000001", "INNH", "Hansen, Ola")
    _insert_enhet(conn, "200000002", "AS", regdato="2026-05-01", kommune="3201")
    _insert_rolle(conn, "200000002", "LEDE", "Hansen, Ola")

    matches = find_enk_conversions(conn, today=date(2026, 5, 19))
    assert matches["200000002"]["match_strength"] == "name_only"


def test_no_match_when_enk_too_old(conn):
    _insert_enhet(conn, "100000001", "ENK", slettedato="2025-01-01", kommune="0301")
    _insert_rolle(conn, "100000001", "INNH", "Hansen, Ola")
    _insert_enhet(conn, "200000002", "AS", regdato="2026-05-01", kommune="0301")
    _insert_rolle(conn, "200000002", "DAGL", "Hansen, Ola")

    matches = find_enk_conversions(conn, today=date(2026, 5, 19))
    assert "200000002" not in matches


def test_no_match_when_as_too_old(conn):
    _insert_enhet(conn, "100000001", "ENK", slettedato="2026-04-15", kommune="0301")
    _insert_rolle(conn, "100000001", "INNH", "Hansen, Ola")
    _insert_enhet(conn, "200000002", "AS", regdato="2026-01-01", kommune="0301")
    _insert_rolle(conn, "200000002", "DAGL", "Hansen, Ola")

    matches = find_enk_conversions(conn, today=date(2026, 5, 19))
    assert "200000002" not in matches
