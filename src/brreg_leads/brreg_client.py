import time
from typing import Any, Iterator

import httpx

from .config import BRREG_API_BASE, REQUEST_TIMEOUT_SECONDS, THROTTLE_SECONDS, USER_AGENT


class BrregClient:
    def __init__(
        self,
        base_url: str = BRREG_API_BASE,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        throttle: float = THROTTLE_SECONDS,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        self._throttle = throttle
        self._last_request_at = 0.0

    def __enter__(self) -> "BrregClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._throttle:
            time.sleep(self._throttle - elapsed)
        resp = self._client.get(path, params=params)
        self._last_request_at = time.monotonic()
        resp.raise_for_status()
        return resp

    def get_enhet(self, orgnr: str) -> dict[str, Any] | None:
        try:
            resp = self._get(f"/enheter/{orgnr}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 410, 422):
                return None
            raise
        return resp.json()

    def get_roller(self, orgnr: str) -> dict[str, Any] | None:
        try:
            resp = self._get(f"/enheter/{orgnr}/roller")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 410, 422):
                return None
            raise
        return resp.json()

    def iter_new_as(
        self,
        kommunenummer: str,
        registered_from: str,
        registered_to: str | None = None,
        page_size: int = 500,
    ) -> Iterator[dict[str, Any]]:
        page = 0
        while True:
            params: dict[str, Any] = {
                "organisasjonsform": "AS",
                "kommunenummer": kommunenummer,
                "fraRegistreringsdatoEnhetsregisteret": registered_from,
                "size": page_size,
                "page": page,
            }
            if registered_to:
                params["tilRegistreringsdatoEnhetsregisteret"] = registered_to
            data = self._get("/enheter", params=params).json()
            enheter = data.get("_embedded", {}).get("enheter", [])
            if not enheter:
                return
            for e in enheter:
                yield e
            page_info = data.get("page", {})
            total_pages = page_info.get("totalPages", 0)
            page += 1
            if page >= total_pages:
                return

    def iter_active_by_kommune(
        self,
        organisasjonsform: str,
        kommunenummer: str,
        page_size: int = 1000,
    ) -> Iterator[dict[str, Any]]:
        page = 0
        while True:
            params: dict[str, Any] = {
                "organisasjonsform": organisasjonsform,
                "kommunenummer": kommunenummer,
                "size": page_size,
                "page": page,
            }
            data = self._get("/enheter", params=params).json()
            enheter = data.get("_embedded", {}).get("enheter", [])
            if not enheter:
                return
            for e in enheter:
                yield e
            page_info = data.get("page", {})
            total_pages = page_info.get("totalPages", 0)
            page += 1
            if page >= total_pages:
                return

    def iter_oppdateringer(
        self,
        after_id: int | None = None,
        page_size: int = 1000,
        max_batches: int = 500,
    ) -> Iterator[dict[str, Any]]:
        # /oppdateringer/enheter uses cursor-only pagination via `oppdateringsid`.
        # Combining `page=N` with `oppdateringsid` past 10000 results returns HTTP 400.
        cursor = after_id
        for _ in range(max_batches):
            params: dict[str, Any] = {"size": page_size}
            if cursor is not None:
                params["oppdateringsid"] = cursor + 1
            data = self._get("/oppdateringer/enheter", params=params).json()
            updates = data.get("_embedded", {}).get("oppdaterteEnheter", [])
            if not updates:
                return
            max_id_in_batch = cursor or 0
            for u in updates:
                yield u
                uid = int(u["oppdateringsid"])
                if uid > max_id_in_batch:
                    max_id_in_batch = uid
            if len(updates) < page_size:
                return
            cursor = max_id_in_batch
