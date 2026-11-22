"""What really goes over the wire, with the real IMAP library: read-only, and never in the clear."""

import re
from pathlib import Path

import pytest
from conftest import settings_for
from fakes import PASSWORD
from wire import Wire, make_certificate

from second_brain_imap import ids, imap
from second_brain_imap.errors import MailError
from second_brain_imap.mailbox import Mailbox, SearchQuery

READING = {
    b"CAPABILITY", b"STARTTLS", b"LOGIN", b"LIST", b"STATUS", b"EXAMINE", b"UID SEARCH",
    b"UID FETCH", b"LOGOUT",
}  # fmt: skip


@pytest.fixture(scope="module")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[str, str]]:
    directory = tmp_path_factory.mktemp("certificates")
    return {
        "bridge": make_certificate(directory, "bridge"),
        "impostor": make_certificate(directory, "impostor"),
    }


def wired(monkeypatch: pytest.MonkeyPatch, **server_options: object) -> Wire:
    wire = Wire(**server_options)
    monkeypatch.setattr(imap, "IMAPClient", wire.client_class())
    return wire


def pinned(certificates: dict[str, tuple[str, str]], **changes: object):  # type: ignore[no-untyped-def]
    return settings_for(
        **{"imap_tls_cert": certificates["bridge"][0], "imap_timeout": 5, **changes}
    )


# ── read-only ────────────────────────────────────────────────────────────────────────────────────


def test_everything_the_server_does_is_made_of_commands_that_read(
    monkeypatch: pytest.MonkeyPatch, certificates: dict[str, tuple[str, str]]
) -> None:
    wire = wired(monkeypatch, certificate=certificates["bridge"])
    mailbox = Mailbox(pinned(certificates))
    assert [folder.name for folder in mailbox.list_folders()] == ["INBOX", "All Mail"]
    found = mailbox.search(
        SearchQuery(text="hello", sender="alice", recipient="bob", unread_only=True)
    )
    assert [message.subject for message in found.messages] == ["Hello"]
    message = mailbox.read(found.messages[0].id, 0, 100)
    assert message.body == "Plain body." and message.sender == "Alice <alice@example.com>"
    assert mailbox.ping() is True

    assert set(wire.verbs()) == READING
    commands = wire.commands()
    assert not [c for c in commands if b" SELECT " in c.upper()]  # EXAMINE, never SELECT
    for fetch in (c for c in commands if b"UID FETCH" in c):
        # A message body is only ever asked for with PEEK, which leaves \\Seen alone.
        assert set(re.findall(rb"BODY[A-Z.]*", fetch)) <= {b"BODYSTRUCTURE", b"BODY.PEEK"}, fetch
    assert wire.plaintext() and all(
        command.split(b" ", 1)[1].upper().startswith((b"CAPABILITY", b"STARTTLS"))
        for command in wire.plaintext()
    )  # nothing but "what can you do" and "start TLS" before encryption


def test_search_criteria_as_bridge_accepts_them(
    monkeypatch: pytest.MonkeyPatch, certificates: dict[str, tuple[str, str]]
) -> None:
    """Terms as literals; `OR` with its operands in line (Bridge rejects `OR (TO x ) (CC x )`)."""
    wire = wired(monkeypatch, certificate=certificates["bridge"])
    mailbox = Mailbox(pinned(certificates, hide_spam_and_trash=False))
    mailbox.search(SearchQuery(sender="{5}", recipient="bob", text='say "hi"', folder="INBOX"))
    (search,) = [c.split(b" ", 1)[1] for c in wire.commands() if b"UID SEARCH" in c]
    assert search == (
        b'UID SEARCH FROM {3}\r\n{5} TEXT {8}\r\nsay "hi" OR TO {3}\r\nbob CC {3}\r\nbob\r\n'
    )


def test_a_search_term_with_a_line_break_or_an_accent_is_still_one_literal(
    monkeypatch: pytest.MonkeyPatch, certificates: dict[str, tuple[str, str]]
) -> None:
    wire = wired(monkeypatch, certificate=certificates["bridge"])
    mailbox = Mailbox(pinned(certificates, hide_spam_and_trash=False))
    mailbox.search(SearchQuery(subject="x\r\nA1 LOGOUT", folder="INBOX"))
    mailbox.search(SearchQuery(subject="café", folder="INBOX"))
    searches = [c.split(b" ", 1)[1] for c in wire.commands() if b"UID SEARCH" in c]
    assert searches == [
        b"UID SEARCH SUBJECT {11}\r\nx A1 LOGOUT\r\n",
        b"UID SEARCH CHARSET UTF-8 SUBJECT {5}\r\ncaf\xc3\xa9\r\n",
    ]
    assert b"LOGOUT" in wire.verbs() and wire.verbs().count(b"LOGOUT") == 2  # only the real ones


def test_a_mailbox_name_is_quoted_and_only_ever_one_the_server_listed(
    monkeypatch: pytest.MonkeyPatch, certificates: dict[str, tuple[str, str]]
) -> None:
    wire = wired(monkeypatch, certificate=certificates["bridge"])
    mailbox = Mailbox(pinned(certificates))
    mailbox.search(SearchQuery(folder="All Mail"))
    with pytest.raises(Exception, match="No such message or mailbox"):
        mailbox.read(ids.encode(ids.MessageRef('INBOX" (x', 7, 1)), 0, 10)
    examined = {c.split(b" ", 2)[2].strip() for c in wire.commands() if b" EXAMINE " in c}
    assert examined <= {b'"INBOX"', b'"All Mail"', b'"Trash"', b'"Spam"'}


# ── never in the clear, never unverified ─────────────────────────────────────────────────────────


def logins(wire: Wire) -> list[bytes]:
    return [command for command in wire.commands() if b" LOGIN " in command.upper()]


def test_the_pinned_certificate_is_accepted_and_the_password_goes_inside_tls(
    monkeypatch: pytest.MonkeyPatch, certificates: dict[str, tuple[str, str]]
) -> None:
    wire = wired(monkeypatch, certificate=certificates["bridge"])
    client = imap.connect(pinned(certificates))
    client.logout()
    assert len(logins(wire)) == 1 and PASSWORD.encode() in logins(wire)[0]
    assert not [c for c in wire.plaintext() if b"LOGIN" in c.upper()]


@pytest.mark.parametrize(
    ("case", "changes", "server"),
    [
        ("another certificate for the same name", {}, {"certificate": "impostor"}),
        ("no pin: the system's authorities don't know it", {"imap_tls_cert": ""}, {}),
        ("a name the certificate isn't for", {"imap_host": "localhost"}, {}),
        ("the server doesn't offer STARTTLS", {}, {"offer_starttls": False}),
        ("the server answers NO to STARTTLS", {}, {"starttls_answer": b"NO"}),
        ("the server answers BAD to STARTTLS", {}, {"starttls_answer": b"BAD"}),
        (
            "TLS from the first byte, another certificate",
            {"imap_security": "tls"},
            {"certificate": "impostor"},
        ),
        ("TLS from the first byte, no pin", {"imap_security": "tls", "imap_tls_cert": ""}, {}),
    ],
)
def test_no_sign_in_is_sent_to_a_server_that_was_not_verified(
    monkeypatch: pytest.MonkeyPatch,
    certificates: dict[str, tuple[str, str]],
    case: str,
    changes: dict[str, object],
    server: dict[str, object],
) -> None:
    options = {"certificate": certificates["bridge"], **server}
    if isinstance(options["certificate"], str):
        options["certificate"] = certificates[options["certificate"]]
    wire = wired(monkeypatch, **options)
    with pytest.raises(MailError):
        imap.connect(pinned(certificates, **changes))
    assert logins(wire) == [], case
    assert not [c for c in wire.commands() if PASSWORD.encode() in c], case


def test_tls_from_the_first_byte_with_the_pinned_certificate(
    monkeypatch: pytest.MonkeyPatch, certificates: dict[str, tuple[str, str]]
) -> None:
    wire = wired(monkeypatch, certificate=certificates["bridge"])
    imap.connect(pinned(certificates, imap_security="tls")).logout()
    assert len(logins(wire)) == 1 and wire.plaintext() == []


def test_a_pin_trusts_that_certificate_and_nothing_else(
    certificates: dict[str, tuple[str, str]],
) -> None:
    context = imap.tls_context(pinned(certificates))
    assert context.verify_mode.name == "CERT_REQUIRED" and context.check_hostname is True
    assert context.cert_store_stats()["x509"] == 1  # not the system's authorities as well
    assert Path(certificates["bridge"][0]).read_text().count("BEGIN CERTIFICATE") == 1


def test_pin_cert_fetches_the_certificate_without_signing_in(
    monkeypatch: pytest.MonkeyPatch, certificates: dict[str, tuple[str, str]]
) -> None:
    import ssl

    wire = wired(monkeypatch, certificate=certificates["bridge"])
    der = imap.fetch_certificate(settings_for(imap_timeout=5))
    assert (
        ssl.DER_cert_to_PEM_cert(der).strip() == Path(certificates["bridge"][0]).read_text().strip()
    )
    assert logins(wire) == []
