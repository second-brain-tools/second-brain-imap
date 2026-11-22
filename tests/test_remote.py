"""Tests for HTTP mode: options, start-up guardrails, and the app over real HTTP."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from fakes import FakeServer
from fastmcp import FastMCP
from fastmcp.utilities.tests import ASGIServer, asgi_server
from second_brain_core.remote import HttpOptions, http_app_kwargs, http_options, run_http

from second_brain_imap.identity import SERVER
from second_brain_imap.server import _mail_answers, create_server


def opts(**kwargs: Any) -> HttpOptions:
    return HttpOptions(**{"host": "127.0.0.1", "port": SERVER.default_port, **kwargs})


# The options and guardrails shared by every server are second-brain-core's, and
# tested there.


def test_this_servers_defaults() -> None:
    assert http_options(SERVER, environ={}) == opts()
    assert opts().port == 8776
    assert http_options(SERVER, environ={"SBI_PORT": "9001"}).port == 9001


# -- the app ------------------------------------------------------------------


def test_app_kwargs_are_stateless_json_with_strict_hosts() -> None:
    kwargs = http_app_kwargs(opts(public_url="https://mail.example.com"))
    assert kwargs == {
        "path": "/mcp",
        "stateless_http": True,
        "json_response": True,
        "host_origin_protection": True,
        "allowed_hosts": ["mail.example.com"],
    }
    assert http_app_kwargs(opts())["allowed_hosts"] == []


def test_run_http_passes_the_same_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    server = create_server()
    seen: dict[str, Any] = {}
    monkeypatch.setattr(server, "run", lambda **kwargs: seen.update(kwargs))
    options = opts(host="::1", port=9400, no_auth=True)
    run_http(server, options)
    assert seen == {
        "transport": "http",
        "host": "::1",
        "port": 9400,
        "show_banner": False,
        "uvicorn_config": {"access_log": False},
        **http_app_kwargs(options),
    }


@asynccontextmanager
async def serving(options: HttpOptions, server: FastMCP | None = None) -> AsyncIterator[ASGIServer]:
    kwargs = http_app_kwargs(options)
    path = kwargs.pop("path")
    async with asgi_server(server or create_server(), path=path, **kwargs) as running:
        yield running


def raw_client(running: ASGIServer, host: str | None = None) -> httpx2.AsyncClient:
    """A plain HTTP client into the app, connected as uvicorn would be: on 127.0.0.1.

    ``host`` changes only the Host header. (The ASGI ``server`` address comes from
    the base URL, and the guard always trusts the address it is bound to, so a test
    that put another name there would be testing nothing.)
    """
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=running.app),
        base_url="http://127.0.0.1",
        headers={"Host": host} if host else None,
    )


async def test_round_trip_over_http(live: FakeServer) -> None:
    async with serving(opts(no_auth=True)) as running, running.client() as client:
        names = {tool.name for tool in await client.list_tools()}
        result = await client.call_tool("mail_list_folders", {})
    assert names == {"mail_list_folders", "mail_search", "mail_recent", "mail_read"}
    assert result.is_error is False


async def test_stateless_requests_need_no_session() -> None:
    """Two separate clients, no shared session: each request stands alone."""
    async with serving(opts(no_auth=True)) as running:
        for _ in range(2):
            async with running.client() as client:
                assert await client.list_tools()


async def test_healthz_up(live: FakeServer) -> None:
    async with serving(opts(no_auth=True)) as running, raw_client(running) as http:
        response = await http.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"server": True, "mail": True}


@pytest.mark.parametrize("how", ["down", "refuse_login"])
async def test_healthz_mail_down(live: FakeServer, how: str) -> None:
    setattr(live, how, True)
    async with serving(opts(no_auth=True)) as running, raw_client(running) as http:
        response = await http.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"server": True, "mail": False}


def test_the_health_check_reads_unusable_settings_as_down(
    live: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _mail_answers() is True
    monkeypatch.setenv("SBI_IMAP_PORT", "not-a-port")
    assert _mail_answers() is False
    monkeypatch.setenv("SBI_IMAP_PORT", "0")
    assert _mail_answers() is False


async def test_healthz_does_not_sign_in_on_every_request(live: FakeServer) -> None:
    """It is outside sign-in, so a flood of requests must not become a flood of logins."""
    async with serving(opts(no_auth=True)) as running, raw_client(running) as http:
        for _ in range(5):
            assert (await http.get("/healthz")).status_code == 200
        assert live.connections == 1
        live.down = True
        assert (await http.get("/healthz")).status_code == 200  # still the remembered answer
    assert live.connections == 1


async def test_healthz_asks_again_once_its_answer_is_old(
    live: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("second_brain_imap.server.HEALTH_CACHE_SECONDS", 0.0)
    async with serving(opts(no_auth=True)) as running, raw_client(running) as http:
        assert (await http.get("/healthz")).status_code == 200
        live.down = True
        assert (await http.get("/healthz")).status_code == 503


async def test_unknown_host_header_is_refused(live: FakeServer) -> None:
    options = opts(public_url="https://mail.example.com")
    async with serving(options) as running:
        async with raw_client(running, "evil.example") as http:
            assert (await http.get("/healthz")).status_code == 421
        async with raw_client(running, "mail.example.com") as http:
            assert (await http.get("/healthz")).status_code == 200
