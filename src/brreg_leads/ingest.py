import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from . import classify, matcher
from .brreg_client import BrregClient
from .config import KOMMUNER
from .db import connect, get_state, init_db, set_state

log = logging.getLogger(__name__)

LAST_POLL_KEY = "last_new_as_poll_date"
LAST_OPPDATERING_KEY = "last_oppdatering_id"
ENK_SEEDED_KEY = "enk_seeded_at"
DEFAULT_BACKFILL_DAYS = 30


@dataclass
class IngestSummary:
    new_as_seen: int = 0
    updates_seen: int = 0
    roller_fetched: int = 0
    leads_upserted: int = 0
    enk_conversions: int = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _extract(enhet: dict[str, Any]) -> dict[str, Any]:
    naering = enhet.get("naeringskode1") or {}
    forretning = enhet.get("forretningsadresse") or {}
    return {
        "orgnr": enhet["organisasjonsnummer"],
        "navn": enhet.get("navn", ""),
        "organisasjonsform": (enhet.get("organisasjonsform") or {}).get("kode", ""),
        "registreringsdato": enhet.get("registreringsdatoEnhetsregisteret"),
        "slettedato": enhet.get("slettedato"),
        "kommunenummer": forretning.get("kommunenummer"),
        "kommune_navn": forretning.get("kommune"),
        "forretningsadresse_json": json.dumps(forretning, ensure_ascii=False),
        "postadresse_json": json.dumps(enhet.get("postadresse") or {}, ensure_ascii=False),
        "naeringskode1_kode": naering.get("kode"),
        "naeringskode1_beskrivelse": naering.get("beskrivelse"),
        "epost": enhet.get("epostadresse"),
        "telefon": enhet.get("telefon"),
        "mobil": enhet.get("mobil"),
        "hjemmeside": enhet.get("hjemmeside"),
        "antall_ansatte": enhet.get("antallAnsatte"),
        "konkurs": 1 if enhet.get("konkurs") else 0,
        "under_avvikling": 1 if enhet.get("underAvvikling") else 0,
        "raw_json": json.dumps(enhet, ensure_ascii=False),
    }


def upsert_enhet(conn: sqlite3.Connection, enhet: dict[str, Any]) -> None:
    fields = _extract(enhet)
    now = _now_iso()
    existing = conn.execute(
        "SELECT first_seen_at FROM enheter WHERE orgnr = ?", (fields["orgnr"],)
    ).fetchone()
    first_seen = existing["first_seen_at"] if existing else now
    conn.execute(
        """
        INSERT INTO enheter (
            orgnr, navn, organisasjonsform, registreringsdato, slettedato,
            kommunenummer, kommune_navn, forretningsadresse_json, postadresse_json,
            naeringskode1_kode, naeringskode1_beskrivelse,
            epost, telefon, mobil, hjemmeside, antall_ansatte,
            konkurs, under_avvikling, raw_json, first_seen_at, last_refreshed_at
        ) VALUES (
            :orgnr, :navn, :organisasjonsform, :registreringsdato, :slettedato,
            :kommunenummer, :kommune_navn, :forretningsadresse_json, :postadresse_json,
            :naeringskode1_kode, :naeringskode1_beskrivelse,
            :epost, :telefon, :mobil, :hjemmeside, :antall_ansatte,
            :konkurs, :under_avvikling, :raw_json, :first_seen_at, :last_refreshed_at
        )
        ON CONFLICT(orgnr) DO UPDATE SET
            navn = excluded.navn,
            organisasjonsform = excluded.organisasjonsform,
            registreringsdato = excluded.registreringsdato,
            slettedato = excluded.slettedato,
            kommunenummer = excluded.kommunenummer,
            kommune_navn = excluded.kommune_navn,
            forretningsadresse_json = excluded.forretningsadresse_json,
            postadresse_json = excluded.postadresse_json,
            naeringskode1_kode = excluded.naeringskode1_kode,
            naeringskode1_beskrivelse = excluded.naeringskode1_beskrivelse,
            epost = excluded.epost,
            telefon = excluded.telefon,
            mobil = excluded.mobil,
            hjemmeside = excluded.hjemmeside,
            antall_ansatte = excluded.antall_ansatte,
            konkurs = excluded.konkurs,
            under_avvikling = excluded.under_avvikling,
            raw_json = excluded.raw_json,
            last_refreshed_at = excluded.last_refreshed_at
        """,
        {**fields, "first_seen_at": first_seen, "last_refreshed_at": now},
    )


def upsert_roller(conn: sqlite3.Connection, orgnr: str, roller_payload: dict[str, Any] | None) -> int:
    if not roller_payload:
        return 0
    now = _now_iso()
    count = 0
    for rg in roller_payload.get("rollegrupper", []) or []:
        for rolle in rg.get("roller", []) or []:
            rt = (rolle.get("type") or {}).get("kode")
            if not rt:
                continue
            if rolle.get("fratraadt"):
                continue
            person = rolle.get("person") or {}
            navn_obj = person.get("navn") or {}
            navn = " ".join(
                filter(None, [navn_obj.get("fornavn"), navn_obj.get("mellomnavn"), navn_obj.get("etternavn")])
            ).strip()
            if not navn:
                enhet_navn = (rolle.get("enhet") or {}).get("navn")
                if isinstance(enhet_navn, list):
                    navn = " ".join(s for s in enhet_navn if s).strip()
                elif isinstance(enhet_navn, str):
                    navn = enhet_navn.strip()
            if not navn:
                continue
            conn.execute(
                """
                INSERT INTO roller (orgnr, rolle_type, person_navn, fra_dato, fetched_at)
                VALUES (?, ?, ?, NULL, ?)
                ON CONFLICT(orgnr, rolle_type, person_navn) DO UPDATE SET
                    fetched_at = excluded.fetched_at
                """,
                (orgnr, rt, navn, now),
            )
            count += 1
    return count


def record_oppdatering(conn: sqlite3.Connection, update: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO oppdateringer (oppdateringsid, orgnr, endringstype, dato)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(oppdateringsid) DO NOTHING
        """,
        (
            int(update["oppdateringsid"]),
            update["organisasjonsnummer"],
            update.get("endringstype"),
            update["dato"],
        ),
    )


def _last_oppdatering_dato(conn: sqlite3.Connection, orgnr: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(dato) AS d FROM oppdateringer WHERE orgnr = ?", (orgnr,)
    ).fetchone()
    return row["d"] if row else None


def upsert_lead(conn: sqlite3.Connection, orgnr: str, cohorts: list[str], score: int) -> None:
    if not cohorts:
        conn.execute("DELETE FROM leads WHERE orgnr = ?", (orgnr,))
        return
    now = _now_iso()
    cohorts_json = json.dumps(cohorts)
    conn.execute(
        """
        INSERT INTO leads (orgnr, cohorts_json, score, status, notes, created_at, updated_at)
        VALUES (?, ?, ?, 'new', NULL, ?, ?)
        ON CONFLICT(orgnr) DO UPDATE SET
            cohorts_json = excluded.cohorts_json,
            score = excluded.score,
            updated_at = excluded.updated_at
        """,
        (orgnr, cohorts_json, score, now, now),
    )


def _reclassify_all(
    conn: sqlite3.Connection,
    enk_matches: dict[str, dict],
    today: date,
) -> int:
    rows = conn.execute(
        """
        SELECT orgnr, organisasjonsform, hjemmeside, naeringskode1_kode,
               konkurs, under_avvikling, slettedato
        FROM enheter
        WHERE organisasjonsform = 'AS'
        """
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


def _pull_new_as(
    client: BrregClient,
    conn: sqlite3.Connection,
    since: str,
    until: str | None,
    kommuner: Iterable[str],
) -> tuple[int, list[str]]:
    seen = 0
    new_as_orgnrs: list[str] = []
    for k in kommuner:
        log.info("Pulling new AS for kommune %s since %s", k, since)
        for enhet in client.iter_new_as(k, registered_from=since, registered_to=until):
            existed = conn.execute(
                "SELECT 1 FROM enheter WHERE orgnr = ?", (enhet["organisasjonsnummer"],)
            ).fetchone()
            upsert_enhet(conn, enhet)
            seen += 1
            if not existed:
                new_as_orgnrs.append(enhet["organisasjonsnummer"])
    return seen, new_as_orgnrs


def _pull_oppdateringer(
    client: BrregClient,
    conn: sqlite3.Connection,
    after_id: int | None,
    target_kommuner: set[str],
) -> tuple[int, int | None, set[str]]:
    seen = 0
    last_id = after_id
    refetch_orgnrs: set[str] = set()
    for update in client.iter_oppdateringer(after_id=after_id):
        record_oppdatering(conn, update)
        seen += 1
        last_id = max(last_id or 0, int(update["oppdateringsid"]))
        orgnr = update["organisasjonsnummer"]
        existing = conn.execute(
            "SELECT kommunenummer FROM enheter WHERE orgnr = ?", (orgnr,)
        ).fetchone()
        if existing and existing["kommunenummer"] in target_kommuner:
            refetch_orgnrs.add(orgnr)
    return seen, last_id, refetch_orgnrs


def _fetch_roller_for_deleted_enks(
    client: BrregClient,
    conn: sqlite3.Connection,
    candidate_orgnrs: Iterable[str],
) -> int:
    fetched = 0
    for orgnr in candidate_orgnrs:
        row = conn.execute(
            "SELECT organisasjonsform, slettedato FROM enheter WHERE orgnr = ?",
            (orgnr,),
        ).fetchone()
        if not row or row["organisasjonsform"] != "ENK" or not row["slettedato"]:
            continue
        already = conn.execute(
            "SELECT 1 FROM roller WHERE orgnr = ? LIMIT 1", (orgnr,)
        ).fetchone()
        if already:
            continue
        log.info("Fetching roller for deleted ENK %s", orgnr)
        roller = client.get_roller(orgnr)
        fetched += upsert_roller(conn, orgnr, roller)
    return fetched


def seed_enk(kommuner: list[str] | None = None) -> int:
    init_db()
    target_kommuner = list(kommuner or KOMMUNER)
    count = 0
    with connect() as conn, BrregClient() as client:
        for k in target_kommuner:
            log.info("Seeding active ENKs for kommune %s", k)
            for enhet in client.iter_active_by_kommune("ENK", k):
                upsert_enhet(conn, enhet)
                count += 1
        set_state(conn, ENK_SEEDED_KEY, _now_iso())
    return count


def run_ingest(
    since: str | None = None,
    until: str | None = None,
    kommuner: list[str] | None = None,
    skip_oppdateringer: bool = False,
) -> IngestSummary:
    init_db()
    summary = IngestSummary()
    today = date.today()
    target_kommuner = list(kommuner or KOMMUNER)
    target_set = set(target_kommuner)

    with connect() as conn, BrregClient() as client:
        if since is None:
            stored = get_state(conn, LAST_POLL_KEY)
            since = stored or (today - timedelta(days=DEFAULT_BACKFILL_DAYS)).isoformat()

        seen, new_as_orgnrs = _pull_new_as(client, conn, since, until, target_kommuner)
        summary.new_as_seen = seen

        if not skip_oppdateringer:
            after_id_str = get_state(conn, LAST_OPPDATERING_KEY)
            after_id = int(after_id_str) if after_id_str else None
            up_seen, last_id, refetch = _pull_oppdateringer(client, conn, after_id, target_set)
            summary.updates_seen = up_seen
            if last_id is not None:
                set_state(conn, LAST_OPPDATERING_KEY, str(last_id))
            for orgnr in refetch:
                enhet = client.get_enhet(orgnr)
                if enhet:
                    upsert_enhet(conn, enhet)
            summary.roller_fetched += _fetch_roller_for_deleted_enks(client, conn, refetch)

        for orgnr in new_as_orgnrs:
            roller = client.get_roller(orgnr)
            summary.roller_fetched += upsert_roller(conn, orgnr, roller)

        enk_matches = matcher.find_enk_conversions(conn, today=today)
        summary.enk_conversions = len(enk_matches)
        summary.leads_upserted = _reclassify_all(conn, enk_matches, today)

        set_state(conn, LAST_POLL_KEY, today.isoformat())

    return summary
