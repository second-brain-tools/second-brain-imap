"""What HTTP mode logs for this server: the call, and never its content or a credential.

The audit line, the rate limit and the logging set-up are second-brain-core's and
tested there; these run them on this server's mail.
"""

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fakes import PASSWORD, FakeServer
from fastmcp import Client
from fastmcp.server.auth import StaticTokenVerifier
from fastmcp.utilities.tests import ASGIServer, asgi_server
from second_brain_core.audit import NO_DEBUG_LOGGERS, QUIET_LOGGERS
from second_brain_core.audit import configure_logging as core_configure_logging
from second_brain_core.remote import HttpOptions, http_app_kwargs

from second_brain_imap import ids
from second_brain_imap.identity import SERVER
from second_brain_imap.server import create_server


def configure_logging() -> None:
    core_configure_logging(SERVER, no_debug=("imapclient",))


# A search term (what somebody is looking for) and what the search brings back.
QUERY = "billing@acme.example"
SUBJECT = "Invoice 42 from Acme"
TOKENS = {
    "owner": {"client_id": "c", "scopes": [], "sub": "65598"},
    "other": {"client_id": "c", "scopes": [], "sub": "2"},
}


@pytest.fixture(autouse=True)
def _restore_loggers() -> Any:
    names = ("second_brain_imap", "imapclient", *QUIET_LOGGERS, *NO_DEBUG_LOGGERS)
    saved = {n: (logging.getLogger(n).level, list(logging.getLogger(n).handlers)) for n in names}
    yield
    for name, (level, handlers) in saved.items():
        logging.getLogger(name).setLevel(level)
        logging.getLogger(name).handlers[:] = handlers


@asynccontextmanager
async def serving(rate: int = 0) -> AsyncIterator[ASGIServer]:
    server = create_server(
        auth=StaticTokenVerifier(tokens=TOKENS),
        allowed_ids=frozenset({"65598", "2"}),
        rate_limit_per_minute=rate,
    )
    options = HttpOptions(host="127.0.0.1", port=8776, public_url="https://mail.example.com")
    kwargs = http_app_kwargs(options)
    path = kwargs.pop("path")
    async with asgi_server(server, path=path, **kwargs) as running:
        yield running


async def _search_as_john(running: ASGIServer) -> None:
    async with running.client(auth="owner") as client:
        result = await client.call_tool("mail_search", {"sender": QUERY})
        assert SUBJECT in str(result.structured_content)


def test_configure_logging_holds_the_loggers_that_print_mail_at_info() -> None:
    # imapclient at DEBUG prints every command and answer: this server names it.
    for name in (*NO_DEBUG_LOGGERS, "imapclient"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    configure_logging()
    for name in (*NO_DEBUG_LOGGERS, "imapclient"):
        assert logging.getLogger(name).level == logging.INFO
    # A quieter level is the operator's choice and is left alone.
    logging.getLogger("fastmcp").setLevel(logging.ERROR)
    configure_logging()
    assert logging.getLogger("fastmcp").level == logging.ERROR


async def test_fastmcp_at_debug_would_print_tool_arguments(live: FakeServer) -> None:
    """The control: at DEBUG, FastMCP logs each call with its arguments, which say
    what is being looked for. configure_logging keeps its loggers out of DEBUG."""
    printed: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            printed.append(record.getMessage())

    fastmcp_logger = logging.getLogger("fastmcp")
    fastmcp_logger.addHandler(Collect(level=logging.DEBUG))

    async def search() -> None:
        async with Client(create_server()) as client:
            await client.call_tool("mail_search", {"sender": QUERY})

    fastmcp_logger.setLevel(logging.DEBUG)
    await search()
    assert any(QUERY in line for line in printed)  # the leak is real

    printed.clear()
    configure_logging()
    await search()
    assert not any(QUERY in line for line in printed)


async def test_logs_hold_the_call_but_no_content_or_credentials(
    live: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    configure_logging()
    caplog.set_level(logging.INFO)
    async with serving() as running:
        await _search_as_john(running)
    text = caplog.text
    assert re.search(r"tool=mail_search caller=65598 client=\S+ outcome=ok", text)
    for secret in (PASSWORD, QUERY, SUBJECT, "Acme", "Bearer john"):
        assert secret not in text, secret


async def test_failed_calls_are_logged_by_type_not_message(
    live: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    unknown = ids.encode(ids.MessageRef("Trash", 1000, 1))
    configure_logging()
    caplog.set_level(logging.INFO, logger="second_brain_imap")
    async with serving() as running, running.client(auth="owner") as client:
        result = await client.call_tool("mail_read", {"message_id": unknown}, raise_on_error=False)
    assert result.is_error
    audit = [r.getMessage() for r in caplog.records if r.name == "second_brain_imap.audit"]
    assert audit and re.match(
        r"tool=mail_read caller=65598 client=\S+ outcome=error:MailNotFoundError ", audit[0]
    )
    assert unknown not in " ".join(audit) and "Trash" not in " ".join(audit)
