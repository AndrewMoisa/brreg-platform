"""Integration tests for the ENK→AS conversion pipeline.

Covers the lazy-roller hook (`_fetch_roller_for_deleted_enks`) and the
end-to-end flow through `matcher.find_enk_conversions` against an in-memory
SQLite database, using a fake Brreg client.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from brreg_leads import ingest, matcher
from brreg_leads.db import SCHEMA


class FakeClient:
    def __init__(self, roller_by_orgnr: dict[str, dict]):
        self._roller = roller_by_orgnr
        self.calls: list[str] = []

    def get_roller(self, orgnr: str):
        self.calls.append(orgnr)
        return self._roller.get(orgnr)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    yield c
    c.close()


def _insert_enhet(conn, orgnr, form, *, regdato=None, slettedato=None, kommune="0301"):
    payload = {"organisasjonsnummer": orgnr, "navn": f"Test {orgnr}"}
    conn.execute(
        """
        INSERT INTO enheter (orgnr, navn, organisasjonsform, registreringsdato, slettedato,
                             kommunenummer, raw_json, first_seen_at, last_refreshed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, '2026-05-19', '2026-05-19')
        """,
        (orgnr, f"Test {orgnr}", form, regdato, slettedato, kommune, json.dumps(payload)),
    )


def _insert_rolle(conn, orgnr, rolle_type, person_navn):
    conn.execute(
        "INSERT INTO roller (orgnr, rolle_type, person_navn, fetched_at) VALUES (?, ?, ?, '2026-05-19')",
        (orgnr, rolle_type, person_navn),
    )


def _roller_payload_for_innehaver(fornavn: str, etternavn: str) -> dict:
    return {
        "rollegrupper": [
            {
                "roller": [
                    {
                        "type": {"kode": "INNH"},
                        "person": {"navn": {"fornavn": fornavn, "etternavn": etternavn}},
                        "fratraadt": False,
                    }
                ]
            }
        ]
    }


def test_lazy_roller_fetches_only_for_deleted_enks(conn):
    _insert_enhet(conn, "100000001", "ENK", slettedato="2026-05-10")
    _insert_enhet(conn, "100000002", "ENK")  # still active — must not fetch
    _insert_enhet(conn, "100000003", "AS", regdato="2026-05-15")  # not ENK — must not fetch

    client = FakeClient({"100000001": _roller_payload_for_innehaver("Ola", "Hansen")})
    fetched = ingest._fetch_roller_for_deleted_enks(
        client, conn, ["100000001", "100000002", "100000003"]
    )

    assert client.calls == ["100000001"], "should only fetch the deleted ENK"
    assert fetched == 1
    stored = conn.execute(
        "SELECT rolle_type, person_navn FROM roller WHERE orgnr = ?", ("100000001",)
    ).fetchone()
    assert stored["rolle_type"] == "INNH"
    assert "Ola" in stored["person_navn"]


def test_lazy_roller_skips_if_already_fetched(conn):
    _insert_enhet(conn, "100000001", "ENK", slettedato="2026-05-10")
    _insert_rolle(conn, "100000001", "INNH", "Hansen, Ola")

    client = FakeClient({"100000001": _roller_payload_for_innehaver("Other", "Person")})
    fetched = ingest._fetch_roller_for_deleted_enks(client, conn, ["100000001"])

    assert client.calls == [], "must not refetch if roller already present"
    assert fetched == 0


def test_end_to_end_match_after_lazy_fetch(conn):
    # Seeded active ENK that has now been re-fetched as deleted
    _insert_enhet(conn, "100000001", "ENK", slettedato="2026-05-10", kommune="0301")
    # Matching new AS, where the deleted ENK's innehaver is now styreleder
    _insert_enhet(conn, "200000002", "AS", regdato="2026-05-15", kommune="0301")
    _insert_rolle(conn, "200000002", "LEDE", "Hansen, Ola")

    client = FakeClient({"100000001": _roller_payload_for_innehaver("Ola", "Hansen")})
    ingest._fetch_roller_for_deleted_enks(client, conn, ["100000001"])

    matches = matcher.find_enk_conversions(conn, today=date(2026, 5, 19))
    assert "200000002" in matches
    assert matches["200000002"]["enk_orgnr"] == "100000001"
