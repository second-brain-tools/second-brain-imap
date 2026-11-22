"""Shared test fixtures: a small mailbox laid out the way Proton Mail Bridge lays one out."""

from datetime import datetime

import pytest
from fakes import PASSWORD, USERNAME, FakeServer, make_message, when

from second_brain_imap import imap
from second_brain_imap.config import Settings
from second_brain_imap.mailbox import Mailbox

PDF = b"%PDF-1.7 " + b"x" * 3000


def deliver(
    server: FakeServer,
    raw: bytes,
    received: datetime,
    *folders: str,
    flags: tuple[bytes, ...] = (b"\\Seen",),
) -> None:
    """Put one message in ``folders`` and, as Bridge does with every message, in All Mail."""
    for name in (*folders, "All Mail"):
        server.folders[name].add(raw, received, flags=flags)


@pytest.fixture
def bridge() -> FakeServer:
    """Folders, labels and the views Bridge adds, with a handful of messages.

    INBOX: the invoice (unread, labelled Todo) and the hello (starred).
    Sent: the reply. Archive: the old receipt. Folders/Receipts: the PDF receipt.
    Spam: the lottery. Trash: the secret plan, which is also labelled Todo.
    All Mail holds a copy of every one of them, Spam and Trash included.
    """
    server = FakeServer()
    server.folder("Folders", b"\\Noselect", b"\\Unmarked")
    server.folder("Sent", b"\\Marked", b"\\Noinferiors", b"\\Sent")
    server.folder("INBOX", b"\\Marked", b"\\Noinferiors")
    server.folder("Labels", b"\\Noselect", b"\\Unmarked")
    server.folder("Trash", b"\\Marked", b"\\Noinferiors", b"\\Trash")
    server.folder("Drafts", b"\\Drafts", b"\\Marked", b"\\Noinferiors")
    server.folder("Spam", b"\\Junk", b"\\Noinferiors", b"\\Unmarked")
    server.folder("Starred", b"\\Flagged", b"\\Noinferiors", b"\\Unmarked")
    server.folder("All Mail", b"\\All", b"\\Marked", b"\\Noinferiors", uidvalidity=2000)
    server.folder("Archive", b"\\Archive", b"\\Noinferiors", b"\\Unmarked")
    server.folder("Folders/Receipts", b"\\Marked")
    server.folder("Labels/Todo", b"\\Marked")

    deliver(
        server,
        make_message(
            subject="Invoice 42 from Acme",
            sender="Acme Billing <billing@acme.example>",
            text="Your invoice for November is attached. Total due: 120 EUR.",
            message_id="<invoice42@acme.example>",
            proton_id="pm-invoice",
        ),
        when(18),
        "INBOX",
        "Labels/Todo",
        flags=(),
    )
    deliver(
        server,
        make_message(
            subject="Hello from Alice",
            text="Lunch on Friday?",
            message_id="<hello@example.com>",
            proton_id="pm-hello",
        ),
        when(17),
        "INBOX",
        "Starred",
        flags=(b"\\Seen", b"\\Flagged"),
    )
    deliver(
        server,
        make_message(
            subject="Re: Hello from Alice",
            sender="Me <me@example.com>",
            to="Alice Example <alice@example.com>",
            text="Friday works.",
            message_id="<reply@example.com>",
            proton_id="pm-reply",
        ),
        when(17, 15),
        "Sent",
    )
    deliver(
        server,
        make_message(
            subject="Receipt for your order",
            sender="Shop <orders@shop.example>",
            text=None,
            html="<html><body><p>Thanks for your <b>order</b>.</p></body></html>",
            message_id="<order1@shop.example>",
            proton_id="pm-order",
        ),
        when(3, month=6),
        "Archive",
    )
    deliver(
        server,
        make_message(
            subject="Your receipt",
            sender="Hotel <stay@hotel.example>",
            text="Receipt attached.",
            attachments=[("receipt.pdf", "application/pdf", PDF)],
            message_id="<stay@hotel.example>",
            proton_id="pm-hotel",
        ),
        when(10),
        "Folders/Receipts",
    )
    deliver(
        server,
        make_message(
            subject="You won the lottery",
            sender="Lottery <win@spam.example>",
            text="Claim your invoice-free prize now.",
            message_id="<spam@spam.example>",
            proton_id="pm-spam",
        ),
        when(19),
        "Spam",
        flags=(),
    )
    deliver(
        server,
        make_message(
            subject="The secret plan",
            sender="Bob <bob@example.com>",
            text="Deleted on purpose: the secret plan invoice.",
            message_id="<secret@example.com>",
            proton_id="pm-secret",
        ),
        when(19, 9),
        "Trash",
        "Labels/Todo",
    )
    return server


def settings_for(**changes: object) -> Settings:
    values: dict[str, object] = {"imap_username": USERNAME, "imap_password": PASSWORD}
    return Settings(**{**values, **changes})  # type: ignore[arg-type]


@pytest.fixture
def mailbox(bridge: FakeServer) -> Mailbox:
    """The mailbox with the default settings: Spam and Trash hidden, nothing else."""
    return Mailbox(settings_for(), connect=bridge.connect)


@pytest.fixture
def live(bridge: FakeServer, monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    """Point the package's own connect at the fake server, with its sign-in in the environment.

    For tests that go through ``create_server`` or the command line, which build
    their own Mailbox.
    """
    monkeypatch.setattr(imap, "connect", bridge.connect)
    monkeypatch.setenv("SBI_IMAP_USERNAME", USERNAME)
    monkeypatch.setenv("SBI_IMAP_PASSWORD", PASSWORD)
    for name in ("SBI_FOLDERS", "SBI_EXCLUDE_FOLDERS", "SBI_HIDE_SPAM_AND_TRASH"):
        monkeypatch.delenv(name, raising=False)
    return bridge
