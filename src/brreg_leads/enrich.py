"""proff.no contact enrichment.

Brreg leaves epost/telefon empty for ~80% of new ASes. proff.no is a public
Norwegian business directory that mirrors Brreg and often includes contact
details. For each lead we don't already have full contact info on, we fetch
its proff.no page and extract `tel:` / `mailto:` / website links.

Parser intentionally uses regex on link hrefs rather than CSS selectors so
small layout changes don't break it. Fixture-based tests live in
tests/test_enrich.py.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import httpx

from .config import REQUEST_TIMEOUT_SECONDS, USER_AGENT

log = logging.getLogger(__name__)

PROFF_BASE = "https://www.proff.no"
PROFF_THROTTLE_SECONDS = 1.0
PROFF_RETRY_BACKOFF = 1.0

TEL_RE = re.compile(r'href=["\']tel:([+\d\s\-()]+)["\']', re.IGNORECASE)
MAILTO_RE = re.compile(r'href=["\']mailto:([^"\'?\s]+)', re.IGNORECASE)
HJEMMESIDE_RE = re.compile(
    r'href=["\'](https?://(?!(?:www\.)?proff\.no)[^"\']+)["\'][^>]*>(?:[^<]*?hjemmeside|website|nettside|besøk)',
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class EnrichmentResult:
    epost: str | None = None
    telefon: str | None = None
    hjemmeside: str | None = None
    source: str = "none"


def parse_proff_html(html: str) -> EnrichmentResult:
    """Extract phone, email, and website from proff.no entity page HTML."""
    if not html:
        return EnrichmentResult()
    tel_match = TEL_RE.search(html)
    mail_match = MAILTO_RE.search(html)
    site_match = HJEMMESIDE_RE.search(html)
    telefon = _normalize_phone(tel_match.group(1)) if tel_match else None
    epost = mail_match.group(1).strip() if mail_match else None
    hjemmeside = site_match.group(1).strip() if site_match else None
    return EnrichmentResult(
        epost=epost,
        telefon=telefon,
        hjemmeside=hjemmeside,
        source="proff" if (telefon or epost or hjemmeside) else "none",
    )


def _normalize_phone(raw: str) -> str:
    return re.sub(r"\s+", " ", raw).strip()


class ProffClient:
    """Minimal client for proff.no pages with throttling + simple retries."""

    def __init__(self, throttle: float = PROFF_THROTTLE_SECONDS):
        self._client = httpx.Client(
            base_url=PROFF_BASE,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            follow_redirects=True,
        )
        self._throttle = throttle
        self._last_request_at = 0.0
        self._blocked = False

    def __enter__(self) -> "ProffClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch(self, orgnr: str) -> str | None:
        if self._blocked:
            return None
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._throttle:
            time.sleep(self._throttle - elapsed)
        try:
            resp = self._client.get(f"/selskap/-/-/-/{orgnr}/")
        except (httpx.ConnectError, httpx.ReadTimeout) as e:
            log.warning("proff.no fetch failed for %s: %s", orgnr, e)
            return None
        finally:
            self._last_request_at = time.monotonic()
        if resp.status_code == 429:
            log.warning("proff.no rate-limited us; skipping further enrichment this run")
            self._blocked = True
            return None
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            log.warning("proff.no returned %s for %s", resp.status_code, orgnr)
            return None
        return resp.text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_enrichment(conn: sqlite3.Connection, orgnr: str, result: EnrichmentResult) -> None:
    conn.execute(
        """
        INSERT INTO enrichment (orgnr, epost, telefon, hjemmeside, source, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(orgnr) DO UPDATE SET
            epost = excluded.epost,
            telefon = excluded.telefon,
            hjemmeside = excluded.hjemmeside,
            source = excluded.source,
            fetched_at = excluded.fetched_at
        """,
        (orgnr, result.epost, result.telefon, result.hjemmeside, result.source, _now_iso()),
    )


def enrich_orgnrs(
    conn: sqlite3.Connection,
    orgnrs: Iterable[str],
    proff: ProffClient | None = None,
    skip_if_brreg_has_email: bool = True,
) -> dict[str, int]:
    """Enrich each orgnr that's missing contact info. Idempotent; existing
    enrichment rows are refreshed. Returns counts."""
    summary = {"attempted": 0, "enriched": 0, "skipped": 0}
    owns_client = proff is None
    proff = proff or ProffClient()
    try:
        for orgnr in orgnrs:
            row = conn.execute(
                "SELECT epost, telefon FROM enheter WHERE orgnr = ?", (orgnr,)
            ).fetchone()
            if (
                skip_if_brreg_has_email
                and row
                and row["epost"]
                and row["telefon"]
            ):
                summary["skipped"] += 1
                continue
            summary["attempted"] += 1
            html = proff.fetch(orgnr)
            if not html:
                continue
            result = parse_proff_html(html)
            if result.source == "none":
                continue
            upsert_enrichment(conn, orgnr, result)
            summary["enriched"] += 1
    finally:
        if owns_client:
            proff.close()
    return summary
