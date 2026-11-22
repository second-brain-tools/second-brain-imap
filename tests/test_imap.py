"""The connection: always encrypted and verified, and only ever reading."""

import ast
import ssl
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from conftest import settings_for
from fakes import PASSWORD, USERNAME, FakeServer
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError, LoginError
from imapclient.imapclient import literal

import second_brain_imap
from second_brain_imap import imap
from second_brain_imap.errors import (
    AUTH_MESSAGE,
    CONNECTION_MESSAGE,
    MailAuthError,
    MailConnectionError,
    MailNotFoundError,
    MailServerError,
)
from second_brain_imap.imap import ReadOnlySession, Term, batches, section_value

PACKAGE = Path(second_brain_imap.__file__).parent
# What the session may call on the IMAP client: the commands that read, and the two that end
# a connection.
READ_COMMANDS = {
    "login", "logout", "shutdown", "list_folders", "folder_status", "select_folder", "search",
    "fetch",
}  # fmt: skip


def test_the_session_calls_only_the_methods_that_read() -> None:
    """Every call on the IMAP client in imap.py, by name: nothing that stores or deletes."""
    tree = ast.parse((PACKAGE / "imap.py").read_text())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_client"
    }
    assert called and called <= READ_COMMANDS, called - READ_COMMANDS
    connecting = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "client"
    }
    assert connecting == {"starttls", "login", "shutdown", "socket"}


def test_no_other_module_talks_to_the_imap_library() -> None:
    for path in PACKAGE.rglob("*.py"):
        if path.name == "imap.py":
            continue
        source = path.read_text()
        assert "imapclient" not in source.replace('"imapclient"', ""), path
        assert "_client." not in source, path


@pytest.mark.parametrize(
    "item",
    [
        "BODY[]",
        "BODY[1]",
        "RFC822",
        "RFC822.TEXT",
        "BODY.PEEK[]",  # the whole message: never needed, so never allowed
        "BODY.PEEK[TEXT]",
        "BODY.PEEK[1] BODY[2]",
        "BODY.PEEK[HEADER.FIELDS (SUBJECT)] FLAGS",
        "ENVELOPE",
        "BODY.PEEK[0]",
    ],
)
def test_fetch_refuses_items_that_are_not_on_the_list(bridge: FakeServer, item: str) -> None:
    session = ReadOnlySession(bridge.connect(settings_for()))
    session.examine("INBOX")
    with pytest.raises(ValueError, match="not a read-only fetch item"):
        session.fetch([1], ["FLAGS", item])
    assert bridge.sent("fetch") == []


def test_fetch_accepts_what_the_server_uses(bridge: FakeServer) -> None:
    session = ReadOnlySession(bridge.connect(settings_for()))
    session.examine("INBOX")
    items = [
        "FLAGS",
        "INTERNALDATE",
        "RFC822.SIZE",
        "BODYSTRUCTURE",
        "BODY.PEEK[HEADER.FIELDS (DATE FROM X-PM-INTERNAL-ID)]",
        "BODY.PEEK[1]<0.4194304>",
    ]
    answer = session.fetch([1], items)
    assert section_value(answer[1], "BODY[HEADER.FIELDS") is not None
    assert section_value(answer[1], "BODY[1]") is not None
    assert section_value(answer[1], "BODY[7]") is None
    assert session.fetch([], ["FLAGS"]) == {}


def test_what_a_caller_searches_for_is_sent_as_a_literal_never_in_the_command_line(
    bridge: FakeServer,
) -> None:
    """``{5}``, a parenthesis or a quote in a term would otherwise be read as IMAP syntax."""
    session = ReadOnlySession(bridge.connect(settings_for()))
    session.examine("INBOX")
    day = date(2026, 11, 1)
    for text in ("{5}", "a)", "(", '"', "*", "%", "NOT", "plain", "two words", "café"):
        session.search(["TEXT", Term(text), "OR", "TO", Term(text), "CC", Term(text), "SINCE", day])
        (_, wire, _) = bridge.sent("search")[-1]
        assert [wire[index] for index in (0, 2, 3, 5, 7, 8)] == [
            "TEXT",
            "OR",
            "TO",
            "CC",
            "SINCE",
            day,
        ]
        for sent in (wire[1], wire[4], wire[6]):
            assert type(sent) is literal and bytes(sent) == text.encode()


@pytest.mark.parametrize(
    "criteria",
    [
        ["TEXT", "a term that was not marked as one"],
        ["DELETED"],
        ["HEADER", Term("X"), Term("y")],
        ["OR", ["TO", Term("a")], ["CC", Term("a")]],  # nested: Bridge rejects how it is sent
        ["LARGER", 5],
        [b"ALL"],
    ],
)
def test_search_refuses_keys_it_does_not_use_and_values_not_marked_as_terms(
    bridge: FakeServer, criteria: list[Any]
) -> None:
    session = ReadOnlySession(bridge.connect(settings_for()))
    session.examine("INBOX")
    with pytest.raises(ValueError, match="search"):
        session.search(criteria)
    assert bridge.sent("search") == []


class Sparse:
    """A client whose answers leave things out."""

    def __init__(self, status: dict[bytes, Any], examine: dict[bytes, Any]) -> None:
        self.status, self.examine = status, examine

    def folder_status(self, folder: str, what: Any = None) -> dict[bytes, Any]:
        return self.status

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, Any]:
        return self.examine


@pytest.mark.parametrize("missing", [b"MESSAGES", b"UNSEEN", b"UIDNEXT", b"UIDVALIDITY"])
def test_a_count_the_server_left_out_is_not_read_as_zero(missing: bytes) -> None:
    """ "No messages in Trash" would mean nothing to withhold. A missing count refuses."""
    whole = {b"MESSAGES": 3, b"UNSEEN": 1, b"UIDNEXT": 9, b"UIDVALIDITY": 7}
    status = {key: value for key, value in whole.items() if key != missing}
    session = ReadOnlySession(Sparse(status, {b"READ-ONLY": [b""]}))  # type: ignore[arg-type]
    with pytest.raises(MailServerError, match="left out part of an answer"):
        session.status("Trash")
    with pytest.raises(MailServerError, match="left out part of an answer"):
        session.examine("Trash")  # no UIDVALIDITY in that answer
    complete = ReadOnlySession(Sparse(whole, {b"READ-ONLY": [b""], b"UIDVALIDITY": 7}))  # type: ignore[arg-type]
    assert complete.status("Trash").messages == 3 and complete.examine("Trash") == 7


def test_a_goodbye_that_fails_still_closes_the_socket() -> None:
    calls: list[str] = []

    class Gone:
        def logout(self) -> None:
            calls.append("logout")
            raise ConnectionResetError

        def shutdown(self) -> None:
            calls.append("shutdown")
            raise OSError  # and a failing shutdown is swallowed too

    ReadOnlySession(Gone()).close()  # type: ignore[arg-type]
    assert calls == ["logout", "shutdown"]


def test_batches() -> None:
    assert batches([]) == []
    assert batches([5, 3, 3, 9]) == [[3, 5, 9]]
    many = batches(range(1, 1202))
    assert [len(chunk) for chunk in many] == [500, 500, 201] and many[1][0] == 501


def test_status_and_examine(bridge: FakeServer) -> None:
    session = ReadOnlySession(bridge.connect(settings_for()))
    status = session.status("INBOX")
    assert (status.messages, status.unseen, status.uidnext, status.uidvalidity) == (2, 1, 3, 1000)
    assert session.examine("All Mail") == 2000
    bridge.confirm_read_only = False
    with pytest.raises(MailServerError, match="read-only"):
        session.examine("INBOX")


class Broken:
    """A client whose every command fails the same way."""

    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    def __getattr__(self, name: str) -> Any:
        def fail(*args: Any, **kwargs: Any) -> Any:
            raise self.failure

        return fail


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ConnectionResetError("reset"), MailConnectionError),
        (TimeoutError("timed out"), MailConnectionError),
        (ssl.SSLError("bad record"), MailConnectionError),
        (IMAPClientAbortError("socket error: EOF"), MailConnectionError),
        (IMAPClientError("NO no such mailbox"), MailServerError),
        (MailNotFoundError(), MailNotFoundError),
    ],
)
def test_failures_become_this_packages_errors(failure: Exception, expected: type) -> None:
    session = ReadOnlySession(Broken(failure))
    for call in (
        session.folders,
        lambda: session.status("INBOX"),
        lambda: session.examine("INBOX"),
        lambda: session.search(["ALL"]),
        lambda: session.fetch([1], ["FLAGS"]),
    ):
        with pytest.raises(expected) as caught:
            call()
        # The sentence a client sees says nothing of the cause.
        assert "reset" not in str(caught.value) and "no such mailbox" not in str(caught.value)
    session.close()  # a failing goodbye is swallowed


# ── connecting ───────────────────────────────────────────────────────────────────────────────────


class Recorder:
    """Stands in for imapclient.IMAPClient and records how it was used."""

    made: list["Recorder"] = []
    fail_on: str | None = None
    failure: Exception = OSError("nope")

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.host, self.port, self.kwargs = host, port, kwargs
        self.calls: list[tuple[Any, ...]] = []
        self.normalise_times = True
        Recorder.made.append(self)
        self._maybe_fail("connect")

    def _maybe_fail(self, step: str) -> None:
        if Recorder.fail_on == step:
            raise Recorder.failure

    def starttls(self, context: ssl.SSLContext) -> None:
        self.calls.append(("starttls", context))
        self._maybe_fail("starttls")

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", username, password))
        self._maybe_fail("login")

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))
        self._maybe_fail("shutdown")

    def socket(self) -> Any:
        class Sock:
            def getpeercert(self, binary_form: bool = False) -> bytes:
                assert binary_form
                return b"DER-BYTES"

        self.calls.append(("socket",))
        return Sock()


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> type[Recorder]:
    Recorder.made, Recorder.fail_on, Recorder.failure = [], None, OSError("nope")
    monkeypatch.setattr(imap, "IMAPClient", Recorder)
    return Recorder


def test_connect_with_starttls_verifies_and_then_signs_in(recorder: type[Recorder]) -> None:
    client = imap.connect(settings_for(imap_host="127.0.0.1", imap_port=1143, imap_timeout=7.0))
    (made,) = recorder.made
    assert client is made  # type: ignore[comparison-overlap]
    assert (made.host, made.port) == ("127.0.0.1", 1143)
    assert made.kwargs == {"ssl": False, "timeout": 7.0}
    assert [call[0] for call in made.calls] == ["starttls", "login"]  # TLS before the password
    context = made.calls[0][1]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert made.calls[1] == ("login", USERNAME, PASSWORD)
    assert made.normalise_times is False


def test_connect_with_tls_from_the_first_byte(recorder: type[Recorder]) -> None:
    imap.connect(settings_for(imap_security="tls", imap_port=993))
    (made,) = recorder.made
    assert made.kwargs["ssl"] is True
    assert made.kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    assert [call[0] for call in made.calls] == ["login"]


def test_connect_without_a_sign_in_never_connects(recorder: type[Recorder]) -> None:
    for changes in ({"imap_username": ""}, {"imap_password": ""}):
        with pytest.raises(MailAuthError) as caught:
            imap.connect(settings_for(**changes))
        assert str(caught.value) == AUTH_MESSAGE
    assert recorder.made == []


@pytest.mark.parametrize(
    ("step", "failure", "expected"),
    [
        ("connect", ConnectionRefusedError("refused"), MailConnectionError),
        ("starttls", ssl.SSLCertVerificationError("self-signed certificate"), MailConnectionError),
        ("login", LoginError("no such user or invalid password"), MailAuthError),
        ("login", TimeoutError("timed out"), MailConnectionError),
        # A password the library can't send as ASCII is one the server didn't accept.
        (
            "login",
            UnicodeEncodeError("ascii", "pässword", 1, 2, "ordinal not in range"),
            MailAuthError,
        ),
    ],
)
def test_connect_failures(
    recorder: type[Recorder], step: str, failure: Exception, expected: type
) -> None:
    recorder.fail_on, recorder.failure = step, failure
    with pytest.raises(expected) as caught:
        imap.connect(settings_for())
    assert str(caught.value) in (AUTH_MESSAGE, CONNECTION_MESSAGE)
    assert caught.value.__cause__ is failure  # kept for the operator (cli.reason)
    if step != "connect":
        assert recorder.made[0].calls[-1] == ("shutdown",)  # the socket is not left open


def test_a_pinned_certificate_is_the_only_one_trusted(tmp_path: Path) -> None:
    assert imap.tls_context(settings_for()).verify_mode == ssl.CERT_REQUIRED
    missing = settings_for(imap_tls_cert=str(tmp_path / "missing.pem"))
    with pytest.raises(MailConnectionError, match="SBI_IMAP_TLS_CERT"):
        imap.tls_context(missing)
    not_a_cert = tmp_path / "junk.pem"
    not_a_cert.write_text("junk")
    with pytest.raises(MailConnectionError, match="SBI_IMAP_TLS_CERT"):
        imap.tls_context(settings_for(imap_tls_cert=str(not_a_cert)))


def test_fetch_certificate_sends_no_sign_in(recorder: type[Recorder]) -> None:
    assert imap.fetch_certificate(settings_for()) == b"DER-BYTES"
    (made,) = recorder.made
    assert [call[0] for call in made.calls] == ["starttls", "socket", "shutdown"]
    assert made.calls[0][1].verify_mode == ssl.CERT_NONE
    recorder.made.clear()
    imap.fetch_certificate(settings_for(imap_security="tls"))
    assert recorder.made[0].kwargs["ssl"] is True
    assert [call[0] for call in recorder.made[0].calls] == ["socket", "shutdown"]
    recorder.fail_on, recorder.failure = "starttls", OSError("nope")
    with pytest.raises(MailConnectionError):
        imap.fetch_certificate(settings_for())
