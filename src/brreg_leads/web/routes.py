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


def _build_lead_query(
    cohort: str | None,
    status: str | None,
    kommune: str | None,
    naering_prefix: str | None,
    search: str | None,
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
    where_sql = " AND ".join(where) if where else "1=1"
    sql = f"""
        SELECT l.orgnr, l.cohorts_json, l.score, l.status, l.notes, l.last_contacted_at,
               e.navn, e.kommunenummer, e.kommune_navn, e.registreringsdato,
               e.naeringskode1_kode, e.naeringskode1_beskrivelse,
               e.epost, e.telefon, e.hjemmeside, e.antall_ansatte
        FROM leads l
        JOIN enheter e ON e.orgnr = l.orgnr
        WHERE {where_sql}
        ORDER BY l.score DESC, e.registreringsdato DESC
    """
    return sql, params


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    cohort: str | None = None,
    status: str | None = None,
    kommune: str | None = None,
    naering_prefix: str | None = None,
    search: str | None = None,
    limit: int = 200,
):
    sql, params = _build_lead_query(cohort, status, kommune, naering_prefix, search)
    sql += " LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        total = conn.execute("SELECT COUNT(*) AS c FROM leads").fetchone()["c"]
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
                "limit": limit,
            },
        },
    )


@router.get("/lead/{orgnr}", response_class=HTMLResponse)
def lead_detail(request: Request, orgnr: str):
    with connect() as conn:
        lead_row = conn.execute(
            """
            SELECT l.*, e.*
            FROM leads l JOIN enheter e ON e.orgnr = l.orgnr
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
            "all_statuses": LEAD_STATUSES,
        },
    )


@router.post("/lead/{orgnr}/status")
def update_status(orgnr: str, status: str = Form(...), notes: str = Form("")):
    if status not in LEAD_STATUSES:
        return HTMLResponse(f"Invalid status: {status}", status_code=400)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    last_contacted_at = now if status == "contacted" else None
    with connect() as conn:
        if last_contacted_at:
            conn.execute(
                "UPDATE leads SET status = ?, notes = ?, last_contacted_at = ?, updated_at = ? WHERE orgnr = ?",
                (status, notes or None, last_contacted_at, now, orgnr),
            )
        else:
            conn.execute(
                "UPDATE leads SET status = ?, notes = ?, updated_at = ? WHERE orgnr = ?",
                (status, notes or None, now, orgnr),
            )
    return RedirectResponse(url=f"/lead/{orgnr}", status_code=303)


@router.get("/export.csv")
def export_csv(
    cohort: str | None = None,
    status: str | None = None,
    kommune: str | None = None,
    naering_prefix: str | None = None,
    search: str | None = None,
):
    sql, params = _build_lead_query(cohort, status, kommune, naering_prefix, search)
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
