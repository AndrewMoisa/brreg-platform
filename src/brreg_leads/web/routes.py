import csv
import io
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from ..config import COHORT_WEIGHTS, KOMMUNER, LEAD_STATUSES
from ..db import connect

router = APIRouter()

ALL_COHORTS = list(COHORT_WEIGHTS.keys())


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
    if reachable:
        where.append("COALESCE(NULLIF(e.epost, ''), x.epost) IS NOT NULL")
    if orgform:
        where.append("e.organisasjonsform = ?")
        params.append(orgform)
    if website == "has":
        where.append("COALESCE(NULLIF(e.hjemmeside, ''), x.hjemmeside) IS NOT NULL")
    elif website == "none":
        where.append("COALESCE(NULLIF(e.hjemmeside, ''), x.hjemmeside) IS NULL")
    return " AND ".join(where), params


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
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        total = conn.execute("SELECT COUNT(*) AS c FROM leads").fetchone()["c"]
        filtered_total = conn.execute(count_sql, count_params).fetchone()["c"]
        cohort_counts_rows = conn.execute(
            """
            SELECT value AS cohort, COUNT(*) AS n
            FROM leads, json_each(leads.cohorts_json)
            GROUP BY value
            """
        ).fetchall()
    cohort_counts = {r["cohort"]: r["n"] for r in cohort_counts_rows}
    leads = [
        {**dict(r), "cohorts": json.loads(r["cohorts_json"]) if r["cohorts_json"] else []}
        for r in rows
    ]
    return request.app.state.templates.TemplateResponse(
        request,
        "leads.html",
        {
            "leads": leads,
            "total": total,
            "filtered_total": filtered_total,
            "is_truncated": len(leads) < filtered_total,
            "cohort_counts": cohort_counts,
            "all_cohorts": ALL_COHORTS,
            "all_statuses": LEAD_STATUSES,
            "kommuner": KOMMUNER,
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
        },
    )


TODAY_CALL_SCORE_THRESHOLD = 4
TODAY_CALL_REGDATE_DAYS = 14
TODAY_FOLLOWUP_DAYS = 7


@router.get("/today", response_class=HTMLResponse)
def today(request: Request):
    from datetime import date, timedelta
    today_ = date.today()
    call_since = (today_ - timedelta(days=TODAY_CALL_REGDATE_DAYS)).isoformat()
    followup_before = (today_ - timedelta(days=TODAY_FOLLOWUP_DAYS)).isoformat()
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
    with connect() as conn:
        call_rows = conn.execute(
            base_select
            + " WHERE l.status = 'new' AND l.score >= ? AND e.registreringsdato >= ?"
            " ORDER BY l.score DESC, e.registreringsdato DESC LIMIT 100",
            (TODAY_CALL_SCORE_THRESHOLD, call_since),
        ).fetchall()
        followup_rows = conn.execute(
            base_select
            + " WHERE l.status = 'contacted' AND (l.last_contacted_at IS NULL OR l.last_contacted_at < ?)"
            " ORDER BY l.last_contacted_at ASC LIMIT 100",
            (followup_before,),
        ).fetchall()
        interested_rows = conn.execute(
            base_select
            + " WHERE l.status = 'interested' ORDER BY l.score DESC LIMIT 100",
        ).fetchall()

    def _pack(rows):
        return [
            {**dict(r), "cohorts": json.loads(r["cohorts_json"]) if r["cohorts_json"] else []}
            for r in rows
        ]

    return request.app.state.templates.TemplateResponse(
        request,
        "today.html",
        {
            "call_list": _pack(call_rows),
            "followups": _pack(followup_rows),
            "interested": _pack(interested_rows),
            "all_statuses": LEAD_STATUSES,
        },
    )


@router.get("/lead/{orgnr}", response_class=HTMLResponse)
def lead_detail(request: Request, orgnr: str):
    with connect() as conn:
        lead_row = conn.execute(
            """
            SELECT l.*, e.*,
                   x.epost AS enrich_epost,
                   x.telefon AS enrich_telefon,
                   x.hjemmeside AS enrich_hjemmeside,
                   x.source AS enrich_source,
                   x.fetched_at AS enrich_fetched_at
            FROM leads l
            JOIN enheter e ON e.orgnr = l.orgnr
            LEFT JOIN enrichment x ON x.orgnr = l.orgnr
            WHERE l.orgnr = ?
            """,
            (orgnr,),
        ).fetchone()
        if not lead_row:
            return HTMLResponse(f"<p>No lead with orgnr {orgnr}</p>", status_code=404)
        roller = conn.execute(
            "SELECT rolle_type, person_navn, fra_dato FROM roller WHERE orgnr = ?",
            (orgnr,),
        ).fetchall()
        updates = conn.execute(
            "SELECT oppdateringsid, endringstype, dato FROM oppdateringer WHERE orgnr = ? ORDER BY dato DESC",
            (orgnr,),
        ).fetchall()
        notes = conn.execute(
            "SELECT id, body, created_at FROM lead_notes WHERE orgnr = ? ORDER BY created_at DESC, id DESC",
            (orgnr,),
        ).fetchall()
    lead = dict(lead_row)
    lead["cohorts"] = json.loads(lead["cohorts_json"]) if lead.get("cohorts_json") else []
    lead["forretningsadresse"] = json.loads(lead.get("forretningsadresse_json") or "{}")
    lead["postadresse"] = json.loads(lead.get("postadresse_json") or "{}")
    return request.app.state.templates.TemplateResponse(
        request,
        "lead_detail.html",
        {
            "lead": lead,
            "roller": [dict(r) for r in roller],
            "updates": [dict(u) for u in updates],
            "notes": [dict(n) for n in notes],
            "all_statuses": LEAD_STATUSES,
        },
    )


def _append_note(conn, orgnr: str, body: str, now: str) -> None:
    conn.execute(
        "INSERT INTO lead_notes (orgnr, body, created_at) VALUES (?, ?, ?)",
        (orgnr, body, now),
    )


@router.post("/lead/{orgnr}/status")
def update_status(orgnr: str, status: str = Form(...), notes: str = Form("")):
    if status not in LEAD_STATUSES:
        return HTMLResponse(f"Invalid status: {status}", status_code=400)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    last_contacted_at = now if status == "contacted" else None
    with connect() as conn:
        row = conn.execute("SELECT status FROM leads WHERE orgnr = ?", (orgnr,)).fetchone()
        prev_status = row["status"] if row else None
        if last_contacted_at:
            conn.execute(
                "UPDATE leads SET status = ?, last_contacted_at = ?, updated_at = ? WHERE orgnr = ?",
                (status, last_contacted_at, now, orgnr),
            )
        else:
            conn.execute(
                "UPDATE leads SET status = ?, updated_at = ? WHERE orgnr = ?",
                (status, now, orgnr),
            )
        if prev_status != status:
            _append_note(conn, orgnr, f"Status: {prev_status} → {status}", now)
        if notes.strip():
            _append_note(conn, orgnr, notes.strip(), now)
    return RedirectResponse(url=f"/lead/{orgnr}", status_code=303)


@router.post("/lead/{orgnr}/note")
def add_note(orgnr: str, body: str = Form(...)):
    body = body.strip()
    if not body:
        return RedirectResponse(url=f"/lead/{orgnr}", status_code=303)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as conn:
        _append_note(conn, orgnr, body, now)
    return RedirectResponse(url=f"/lead/{orgnr}", status_code=303)


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
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    def stream():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "orgnr", "navn", "kommune_navn", "kommunenummer", "registreringsdato",
            "naeringskode", "naering_beskrivelse", "epost", "telefon", "hjemmeside",
            "antall_ansatte", "cohorts", "score", "status",
        ])
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)
        for r in rows:
            cohorts = json.loads(r["cohorts_json"]) if r["cohorts_json"] else []
            writer.writerow([
                r["orgnr"], r["navn"], r["kommune_navn"], r["kommunenummer"],
                r["registreringsdato"], r["naeringskode1_kode"], r["naeringskode1_beskrivelse"],
                r["epost"] or "", r["telefon"] or "", r["hjemmeside"] or "",
                r["antall_ansatte"] or "", ";".join(cohorts), r["score"], r["status"],
            ])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        stream(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="brreg-leads-{stamp}.csv"'},
    )
