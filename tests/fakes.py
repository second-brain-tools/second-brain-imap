"""An in-memory mail server that answers the way IMAPClient hands Proton Mail Bridge's answers on.

The shapes (BODYSTRUCTURE tuples, the names of fetched items, flags as bytes) were
copied from a live Bridge 3.27.2 through IMAPClient 4.3.0. Every command is
recorded, so a test can say what was sent as well as what came back.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any

from imapclient.imapclient import literal

from second_brain_imap.config import Settings
from second_brain_imap.errors import MailAuthError, MailConnectionError

USERNAME = "me@example.com"
PASSWORD = "bridge-password-SECRET"


def make_message(
    *,
    subject: str = "Hello",
    sender: str = "Alice Example <alice@example.com>",
    to: str = "me@example.com",
    cc: str | None = None,
    text: str | None = "Plain body.",
    html: str | None = None,
    attachments: Sequence[tuple[str, str, bytes]] = (),
    message_id: str | None = "<m1@example.com>",
    proton_id: str | None = None,
    date_header: str = "Mon, 02 Nov 2026 10:00:00 +0000",
) -> bytes:
    """An RFC 822 message as bytes. ``attachments`` are (file name, content type, data)."""
    message = EmailMessage()
    message["Date"] = date_header
    message["From"] = sender
    message["To"] = to
    if cc:
        message["Cc"] = cc
    message["Subject"] = subject
    if message_id:
        message["Message-ID"] = message_id
    if proton_id:
        message["X-Pm-Internal-Id"] = proton_id
    if text is not None:
        message.set_content(text, cte="quoted-printable")
        if html is not None:
            message.add_alternative(html, subtype="html", cte="quoted-printable")
    elif html is not None:
        message.set_content(html, subtype="html", cte="quoted-printable")
    for name, content_type, data in attachments:
        maintype, subtype = content_type.split("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return message.as_bytes()


def _payload(part: EmailMessage) -> bytes:
    """A leaf's content as stored: still base64 or quoted-printable."""
    return part.as_bytes().split(b"\n\n", 1)[1]


def structure_of(part: EmailMessage) -> tuple[Any, ...]:
    """``part`` as IMAPClient's BodyData lays out Bridge's BODYSTRUCTURE."""
    if part.is_multipart():
        children = [structure_of(child) for child in part.iter_parts()]  # type: ignore[arg-type]
        return (
            children,
            part.get_content_subtype().encode(),
            (b"boundary", str(part.get_boundary()).encode()),
            None,
            None,
            None,
        )
    params: list[bytes] = []
    for key, value in part.get_params(failobj=[])[1:]:
        params += [key.encode(), str(value).encode()]
    encoding = str(part.get("Content-Transfer-Encoding", "7bit")).encode()
    size = len(_payload(part))
    filename = part.get_filename()
    disposition: tuple[Any, ...] | None = None
    kind = part.get_content_disposition()
    if kind:
        disposition = (kind.encode(), (b"filename", filename.encode()) if filename else None)
    head = (
        part.get_content_maintype().encode(),
        part.get_content_subtype().encode(),
        tuple(params) or None,
        None,
        None,
        encoding,
        size,
    )
    if part.get_content_maintype() == "text":
        return (*head, _payload(part).count(b"\n"), None, disposition, None, None)
    return (*head, None, disposition, None, None)


def _sections(part: EmailMessage, prefix: str = "") -> dict[str, EmailMessage]:
    """Every leaf by its IMAP section number."""
    if not part.is_multipart():
        return {prefix.rstrip(".") or "1": part}
    found: dict[str, EmailMessage] = {}
    for index, child in enumerate(part.iter_parts(), start=1):
        found.update(_sections(child, f"{prefix}{index}."))  # type: ignore[arg-type]
    return found


@dataclass
class StoredMessage:
    uid: int
    raw: bytes
    internaldate: datetime
    flags: tuple[bytes, ...] = (b"\\Seen",)

    @property
    def parsed(self) -> EmailMessage:
        return BytesParser(policy=policy.default).parsebytes(self.raw)

    def header(self, name: str) -> str:
        return str(self.parsed.get(name, ""))


@dataclass
class StoredFolder:
    name: str
    flags: tuple[bytes, ...] = ()
    uidvalidity: int = 1000
    messages: list[StoredMessage] = field(default_factory=list)
    # The next UID. It only ever goes up, as on a real server: a number is not
    # given out again after its message is gone.
    uidnext: int = 1

    def add(self, raw: bytes, when: datetime, *, flags: tuple[bytes, ...] = (b"\\Seen",)) -> int:
        uid = self.uidnext
        self.uidnext += 1
        self.messages.append(StoredMessage(uid, raw, when, flags))
        return uid


class FakeServer:
    """A mailbox, and a record of every command sent to it."""

    def __init__(self) -> None:
        self.folders: dict[str, StoredFolder] = {}
        self.commands: list[tuple[Any, ...]] = []
        self.connections = 0
        # Ways to misbehave.
        self.down = False
        self.refuse_login = False
        self.confirm_read_only = True
        # (mailbox, uid) pairs whose header fetch comes back without the headers.
        self.headerless: set[tuple[str, int]] = set()
        # Called with the mailbox name each time one is opened: something happening
        # in the mailbox in the middle of a call.
        self.on_select: Callable[[str], None] | None = None

    def folder(self, name: str, *flags: bytes, uidvalidity: int = 1000) -> StoredFolder:
        self.folders[name] = StoredFolder(name, flags, uidvalidity)
        return self.folders[name]

    def connect(self, settings: Settings) -> "FakeClient":
        """What ``imap.connect`` does, against this server."""
        if self.down:
            raise MailConnectionError
        if (
            self.refuse_login
            or settings.imap_username != USERNAME
            or settings.imap_password != PASSWORD
        ):
            raise MailAuthError
        self.connections += 1
        return FakeClient(self)

    def sent(self, command: str) -> list[tuple[Any, ...]]:
        return [entry for entry in self.commands if entry[0] == command]


def _term(value: Any) -> str:
    """A search value, which must have been sent as a literal (imap.Term)."""
    assert isinstance(value, literal), f"the search term {value!r} was not sent as a literal"
    return bytes(value).decode("utf-8").lower()


Predicate = Callable[[StoredMessage], bool]


def _parse(criteria: list[Any], index: int) -> tuple[Predicate, int]:
    """One search key with its operands, starting at ``index``; and where the next begins."""
    key = criteria[index]
    assert not isinstance(key, (list, tuple)), (
        "criteria must be flat: Bridge rejects the nested form"
    )
    if key == "ALL":
        return (lambda message: True), index + 1
    if key == "UNSEEN":
        return (lambda message: b"\\Seen" not in message.flags), index + 1
    if key in ("FROM", "TO", "CC", "SUBJECT"):
        term = _term(criteria[index + 1])
        return (lambda message: term in message.header(key).lower()), index + 2
    if key == "TEXT":
        needle = _term(criteria[index + 1]).encode()
        return (lambda message: needle in message.raw.lower()), index + 2
    if key in ("SINCE", "BEFORE"):
        day: date = criteria[index + 1]
        if key == "SINCE":
            return (lambda message: message.internaldate.date() >= day), index + 2
        return (lambda message: message.internaldate.date() < day), index + 2
    if key == "OR":
        first, after_first = _parse(criteria, index + 1)
        second, after_second = _parse(criteria, after_first)
        return (lambda message: first(message) or second(message)), after_second
    raise AssertionError(f"the fake server doesn't know the search key {key!r}")


def _matches(message: StoredMessage, criteria: list[Any]) -> bool:
    """IMAP SEARCH the way Gluon does it: every key must hold; undecoded text, any letter case."""
    index = 0
    while index < len(criteria):
        predicate, index = _parse(criteria, index)
        if not predicate(message):
            return False
    return True


class FakeClient:
    """One connection: the methods of IMAPClient that the server uses."""

    def __init__(self, server: FakeServer) -> None:
        self._server = server
        self._selected: StoredFolder | None = None

    def login(self, username: str, password: str) -> bytes:  # pragma: no cover - connect signs in
        return b"OK"

    def logout(self) -> bytes:
        self._server.commands.append(("logout",))
        return b"BYE"

    def shutdown(self) -> None:  # pragma: no cover - only after a goodbye that failed
        self._server.commands.append(("shutdown",))

    def list_folders(self) -> list[tuple[Any, Any, Any]]:
        self._server.commands.append(("list",))
        return [(folder.flags, b"/", folder.name) for folder in self._server.folders.values()]

    def folder_status(self, folder: str, what: Any = None) -> dict[bytes, Any]:
        self._server.commands.append(("status", folder))
        stored = self._server.folders[folder]
        unseen = sum(1 for message in stored.messages if b"\\Seen" not in message.flags)
        return {
            b"MESSAGES": len(stored.messages),
            b"UNSEEN": unseen,
            b"UIDNEXT": stored.uidnext,
            b"UIDVALIDITY": stored.uidvalidity,
        }

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, Any]:
        self._server.commands.append(("select", folder, readonly))
        if self._server.on_select is not None:
            self._server.on_select(folder)
        self._selected = self._server.folders[folder]
        answer: dict[bytes, Any] = {
            b"EXISTS": len(self._selected.messages),
            b"UIDVALIDITY": self._selected.uidvalidity,
            b"UIDNEXT": self._selected.uidnext,
        }
        if readonly and self._server.confirm_read_only:
            answer[b"READ-ONLY"] = [b""]
        return answer

    def search(self, criteria: Any = "ALL", charset: str | None = None) -> list[int]:
        self._server.commands.append(("search", criteria, charset))
        assert self._selected is not None, "SEARCH with no mailbox open"
        return [m.uid for m in self._selected.messages if _matches(m, list(criteria))]

    def fetch(self, messages: Any, data: Any) -> dict[int, dict[bytes, Any]]:
        self._server.commands.append(("fetch", tuple(messages), tuple(data)))
        assert self._selected is not None, "FETCH with no mailbox open"
        wanted = {int(uid) for uid in messages}
        result: dict[int, dict[bytes, Any]] = {}
        for sequence, message in enumerate(self._selected.messages, start=1):
            if message.uid not in wanted:
                continue
            items: dict[bytes, Any] = {b"SEQ": sequence}
            for item in data:
                missing = (self._selected.name, message.uid) in self._server.headerless
                if not (missing and item.startswith("BODY.PEEK[HEADER.FIELDS")):
                    self._item(message, item, items)
            result[message.uid] = items
        return result

    @staticmethod
    def _item(message: StoredMessage, item: str, items: dict[bytes, Any]) -> None:
        if item == "FLAGS":
            items[b"FLAGS"] = message.flags
        elif item == "INTERNALDATE":
            items[b"INTERNALDATE"] = message.internaldate
        elif item == "RFC822.SIZE":
            items[b"RFC822.SIZE"] = len(message.raw)
        elif item == "BODYSTRUCTURE":
            items[b"BODYSTRUCTURE"] = structure_of(message.parsed)
        elif item.startswith("BODY.PEEK[HEADER.FIELDS ("):
            names = item[len("BODY.PEEK[HEADER.FIELDS (") : -2].split()
            compat = BytesParser(policy=policy.compat32).parsebytes(message.raw, headersonly=True)
            lines = [
                f"{name}: {value}\r\n".encode("utf-8", errors="surrogateescape")
                for name, value in compat.raw_items()
                if name.upper() in names
            ]
            items[item.replace(".PEEK", "").encode()] = b"".join(lines) + b"\r\n"
        elif item.startswith("BODY.PEEK["):
            section, _, window = item[len("BODY.PEEK[") :].partition("]")
            content = _payload(_sections(message.parsed)[section])
            name = f"BODY[{section}]"
            if window:
                start, length = (int(number) for number in window.strip("<>").split("."))
                content = content[start : start + length]
                name += f"<{start}>"
            items[name.encode()] = content
        else:
            raise AssertionError(f"the fake server was asked for {item!r}")


def when(day: int, hour: int = 12, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, 0, 0, tzinfo=UTC)
