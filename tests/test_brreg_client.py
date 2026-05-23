"""Retry behaviour tests for BrregClient using httpx.MockTransport.

We patch the client's underlying transport so we can simulate 5xx responses,
connection errors, and 4xx errors without hitting the network.
"""

from __future__ import annotations

import httpx
import pytest

from brreg_leads import brreg_client as bc


def _make_client(handler, throttle: float = 0.0) -> bc.BrregClient:
    client = bc.BrregClient(throttle=throttle)
    client._client.close()
    client._client = httpx.Client(
        base_url=bc.BRREG_API_BASE,
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(bc, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(bc.time, "sleep", lambda *_: None)


def test_retries_503_then_succeeds():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, text="upstream broke")
        return httpx.Response(200, json={"organisasjonsnummer": "999999999"})

    client = _make_client(handler)
    result = client.get_enhet("999999999")
    assert result == {"organisasjonsnummer": "999999999"}
    assert len(calls) == 3


def test_gives_up_after_three_attempts_on_persistent_5xx():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(502, text="bad gateway")

    client = _make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_enhet("999999999")
    assert len(calls) == 3


def test_4xx_not_retried_but_404_returns_none():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, text="not found")

    client = _make_client(handler)
    assert client.get_enhet("999999999") is None
    assert len(calls) == 1  # no retry on 404


def test_4xx_not_retried_but_400_raises():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad request")

    client = _make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        # get_enhet handles 404/410/422 but not 400
        client.get_enhet("999999999")
    assert len(calls) == 1


def test_connect_error_retries():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 2:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"organisasjonsnummer": "999999999"})

    client = _make_client(handler)
    result = client.get_enhet("999999999")
    assert result == {"organisasjonsnummer": "999999999"}
    assert len(calls) == 2


def test_iter_new_enheter_passes_organisasjonsform(monkeypatch):
    from brreg_leads.brreg_client import BrregClient

    captured = {}

    class _Resp:
        def json(self):
            return {"_embedded": {"enheter": []}, "page": {"totalPages": 0}}

    def fake_get(self, path, params=None):
        captured["params"] = params
        return _Resp()

    monkeypatch.setattr(BrregClient, "_get", fake_get)
    client = BrregClient()
    list(client.iter_new_enheter("ENK", "0301", registered_from="2026-05-01"))
    assert captured["params"]["organisasjonsform"] == "ENK"
    assert captured["params"]["kommunenummer"] == "0301"
