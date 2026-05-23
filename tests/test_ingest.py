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


def test_replay_oppdateringer_refetches_stranded_enks(conn):
    """A seeded ENK with a stranded oppdateringer event newer than its
    last_refreshed_at gets re-fetched; if the refetch reveals it's now deleted,
    roller are pulled lazily."""
    _insert_enhet(conn, "100000001", "ENK", kommune="0301")
    conn.execute(
        "UPDATE enheter SET last_refreshed_at = '2026-04-01' WHERE orgnr = ?",
        ("100000001",),
    )
    conn.execute(
        "INSERT INTO oppdateringer (oppdateringsid, orgnr, endringstype, dato) "
        "VALUES (?, ?, ?, ?)",
        (1, "100000001", "Ukjent", "2026-05-10"),
    )

    # Brreg now reports it as deleted
    class RefetchingFakeClient(FakeClient):
        def get_enhet(self, orgnr):
            return {
                "organisasjonsnummer": orgnr,
                "navn": f"Test {orgnr}",
                "organisasjonsform": {"kode": "ENK"},
                "slettedato": "2026-05-10",
                "forretningsadresse": {"kommunenummer": "0301", "kommune": "OSLO"},
            }

    client = RefetchingFakeClient({
        "100000001": _roller_payload_for_innehaver("Ola", "Hansen"),
    })
    refetched, roller_added = ingest._replay_oppdateringer_for_kommune_enks(
        client, conn, ["0301"]
    )
    assert refetched == 1
    assert roller_added == 1

    # After replay the entity is marked deleted and has its INNH role
    row = conn.execute(
        "SELECT slettedato FROM enheter WHERE orgnr = ?", ("100000001",)
    ).fetchone()
    assert row["slettedato"] == "2026-05-10"
    role = conn.execute(
        "SELECT rolle_type, person_navn FROM roller WHERE orgnr = ?", ("100000001",)
    ).fetchone()
    assert role["rolle_type"] == "INNH"


def test_replay_skips_enks_already_refreshed_after_event(conn):
    """If last_refreshed_at is newer than the event, no work is needed."""
    _insert_enhet(conn, "100000001", "ENK", kommune="0301")
    conn.execute(
        "UPDATE enheter SET last_refreshed_at = '2026-05-15' WHERE orgnr = ?",
        ("100000001",),
    )
    conn.execute(
        "INSERT INTO oppdateringer (oppdateringsid, orgnr, endringstype, dato) "
        "VALUES (?, ?, ?, ?)",
        (1, "100000001", "Ukjent", "2026-05-10"),
    )

    client = FakeClient({})
    # Need a get_enhet attribute to not error if accidentally called
    client.get_enhet = lambda orgnr: pytest.fail("Should not refetch")  # type: ignore
    refetched, roller_added = ingest._replay_oppdateringer_for_kommune_enks(
        client, conn, ["0301"]
    )
    assert refetched == 0
    assert roller_added == 0


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
