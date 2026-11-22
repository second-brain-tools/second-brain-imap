"""The connection to the IMAP server, and the only commands sent on it.

After signing in, this server sends LIST, STATUS, EXAMINE, UID SEARCH, UID FETCH
and LOGOUT, and nothing else (the IMAP library adds CAPABILITY, which only asks
what the server can do). :class:`ReadOnlySession` is the one place commands
leave from, so "read-only" is a property of this file and not of every caller's
care:

- a mailbox is opened with EXAMINE, which the server itself treats as read-only,
  and a server that answers without confirming it is refused;
- message data is fetched with ``BODY.PEEK``, which never sets the ``\\Seen``
  flag, and any other fetch item is rejected before it is sent;
- what a caller asks to search for is sent as a literal, never as part of the
  command line, so no search term can be read as IMAP syntax;
- there is no method here that stores a flag, appends, copies, moves, expunges,
  creates, renames or deletes.

The connection is always encrypted and the certificate always verified: against
the system's certificate authorities, or against the one certificate named by
``SBI_IMAP_TLS_CERT`` (Proton Mail Bridge's is self-signed). There is no option
to skip either.
"""

import re
import ssl
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, cast

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError, LoginError
from imapclient.imapclient import literal

from .config import Settings
from .errors import (
    MailAuthError,
    MailConnectionError,
    MailError,
    MailServerError,
)
from .scope import FolderInfo, folder_info

_PLAIN_ITEMS = frozenset({"FLAGS", "INTERNALDATE", "RFC822.SIZE", "BODYSTRUCTURE"})
# BODY.PEEK of named header fields, or of one numbered part, optionally a byte range.
_PEEK_ITEM = re.compile(
    r"BODY\.PEEK\[(?:HEADER\.FIELDS \([A-Z0-9 -]+\)|[1-9]\d*(?:\.[1-9]\d*)*)\](?:<\d+\.\d+>)?"
)
# The SEARCH keys this server uses. Anything else in a criteria list is a bug here.
_SEARCH_KEYS = frozenset(
    {"ALL", "UNSEEN", "FROM", "TO", "CC", "SUBJECT", "TEXT", "SINCE", "BEFORE", "OR"}
)
_STATUS_ITEMS = ["MESSAGES", "UNSEEN", "UIDNEXT", "UIDVALIDITY"]
# How many messages one FETCH command names (the library lists each number).
_UIDS_PER_COMMAND = 500

CERT_MESSAGE = (
    "The certificate file named by SBI_IMAP_TLS_CERT can't be read. On the server's "
    "machine, `second-brain-imap pin-cert` writes it."
)
NOT_READ_ONLY_MESSAGE = (
    "The mail server opened a mailbox without confirming it is read-only, so this is refused."
)
REFUSED_MESSAGE = "The mail server refused a request this server made."
INCOMPLETE_MESSAGE = (
    "The mail server left out part of an answer this server depends on, so this is refused."
)


class ImapClient(Protocol):
    """The part of ``imapclient.IMAPClient`` this server uses."""

    def login(self, username: str, password: str) -> Any: ...

    def logout(self) -> Any: ...

    def list_folders(self) -> list[tuple[Any, Any, Any]]: ...

    def folder_status(self, folder: str, what: Any = None) -> dict[bytes, Any]: ...

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, Any]: ...

    def search(self, criteria: Any = "ALL", charset: str | None = None) -> list[int]: ...

    def fetch(self, messages: Any, data: Any) -> dict[int, dict[bytes, Any]]: ...

    def shutdown(self) -> Any: ...


@contextmanager
def _translated() -> Iterator[None]:
    """Turn the IMAP library's and the socket's failures into this package's errors."""
    try:
        yield
    except MailError:
        raise
    except LoginError as exc:
        raise MailAuthError from exc
    except (OSError, IMAPClientAbortError) as exc:  # ssl.SSLError and timeouts are OSErrors
        raise MailConnectionError from exc
    except IMAPClientError as exc:
        raise MailServerError(REFUSED_MESSAGE) from exc


def tls_context(settings: Settings) -> ssl.SSLContext:
    """A context that verifies the server: pinned to one certificate, or the system's CAs."""
    if not settings.imap_tls_cert:
        return ssl.create_default_context()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # verifies, and checks the host name
    try:
        context.load_verify_locations(cafile=settings.imap_tls_cert)
    except (OSError, ssl.SSLError) as exc:
        raise MailConnectionError(CERT_MESSAGE) from exc
    return context


def connect(settings: Settings) -> ImapClient:
    """Open an encrypted, verified, signed-in connection to the configured server."""
    if not settings.imap_username or not settings.imap_password:
        raise MailAuthError
    context = tls_context(settings)
    with _translated():
        if settings.imap_security == "tls":
            client = IMAPClient(
                settings.imap_host,
                settings.imap_port,
                ssl=True,
                ssl_context=context,
                timeout=settings.imap_timeout,
            )
        else:
            client = IMAPClient(
                settings.imap_host, settings.imap_port, ssl=False, timeout=settings.imap_timeout
            )
        try:
            if settings.imap_security == "starttls":
                client.starttls(context)
            # Times as the server gives them, with their offset, not shifted to this
            # machine's zone and stripped of it.
            client.normalise_times = False
            try:
                client.login(settings.imap_username, settings.imap_password)
            except UnicodeError as exc:
                # The library sends the sign-in as ASCII. A username or password it
                # can't send is one the server can't have accepted either.
                raise MailAuthError from exc
        except BaseException:
            with suppress(Exception):
                client.shutdown()
            raise
    return cast(ImapClient, client)


def fetch_certificate(settings: Settings) -> bytes:
    """The certificate the configured server presents, in DER form, without trusting it.

    The one connection here that verifies nothing, for ``pin-cert``: nothing is
    sent on it but the request to start TLS, and no sign-in. The caller decides
    whether to trust what comes back (cli.py: a loopback server only).
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with _translated():
        client = IMAPClient(
            settings.imap_host,
            settings.imap_port,
            ssl=settings.imap_security == "tls",
            ssl_context=context if settings.imap_security == "tls" else None,
            timeout=settings.imap_timeout,
        )
        try:
            if settings.imap_security == "starttls":
                client.starttls(context)
            certificate = client.socket().getpeercert(binary_form=True)
        finally:
            with suppress(Exception):
                client.shutdown()
    if not certificate:
        raise MailConnectionError
    return bytes(certificate)


class Term(str):
    """Text to look for: a value in SEARCH criteria, as opposed to a key such as ``FROM``.

    A term is whatever a caller typed, so it is never placed in the command line,
    where ``{5}``, a parenthesis or a quote would be read as IMAP syntax. It is
    sent as a literal: its length first, then exactly that many bytes, which the
    server takes as the value whatever they are.
    """


def _wire_criteria(criteria: Sequence[Any]) -> list[Any]:
    """``criteria`` as the IMAP library takes them: keys checked, terms made literals.

    A flat list only. The library would wrap a nested list in parentheses, and
    with a literal inside it leaves a space before the closing one, which Bridge
    rejects; ``OR`` takes its two operands in line instead.
    """
    wire: list[Any] = []
    for item in criteria:
        if isinstance(item, Term):
            wire.append(literal(item.encode("utf-8")))
        elif isinstance(item, str):
            if item not in _SEARCH_KEYS:
                raise ValueError(f"not a search key this server uses: {item!r}")
            wire.append(item)
        elif isinstance(item, date):
            wire.append(item)
        else:
            raise ValueError(f"not something to search by: {item!r}")
    return wire


@dataclass(frozen=True)
class FolderStatus:
    messages: int
    unseen: int
    uidnext: int
    uidvalidity: int


def batches(uids: Iterable[int]) -> list[list[int]]:
    """``uids`` in order, without repeats, in lists short enough for one command each."""
    ordered = sorted(set(uids))
    return [
        ordered[index : index + _UIDS_PER_COMMAND]
        for index in range(0, len(ordered), _UIDS_PER_COMMAND)
    ]


def section_value(items: dict[bytes, Any], prefix: str) -> bytes | None:
    """The bytes fetched for a ``BODY[...]`` item, found by how its name starts.

    The server names the item its own way in the answer (without ``.PEEK``, and
    with only the start of a byte range), so it is matched by prefix.
    """
    wanted = prefix.encode()
    for key, value in items.items():
        if key.startswith(wanted) and isinstance(value, bytes):
            return value
    return None


class ReadOnlySession:
    """One signed-in connection, limited to the commands that read."""

    def __init__(self, client: ImapClient) -> None:
        self._client = client

    def folders(self) -> list[FolderInfo]:
        """Every mailbox the server lists."""
        with _translated():
            listed = self._client.list_folders()
        return [folder_info(flags, delimiter, name) for flags, delimiter, name in listed]

    def status(self, folder: str) -> FolderStatus:
        """Message counts and the UID state of ``folder``, without opening it."""
        with _translated():
            answer = self._client.folder_status(folder, _STATUS_ITEMS)
        try:
            return FolderStatus(
                messages=int(answer[b"MESSAGES"]),
                unseen=int(answer[b"UNSEEN"]),
                uidnext=int(answer[b"UIDNEXT"]),
                uidvalidity=int(answer[b"UIDVALIDITY"]),
            )
        except (KeyError, TypeError, ValueError):
            # A count that is missing is not a count of zero: "no messages" in a
            # hidden mailbox would mean nothing to withhold.
            raise MailServerError(INCOMPLETE_MESSAGE) from None

    def examine(self, folder: str) -> int:
        """Open ``folder`` read-only and return its UIDVALIDITY."""
        with _translated():
            answer = self._client.select_folder(folder, readonly=True)
        if b"READ-ONLY" not in answer:
            raise MailServerError(NOT_READ_ONLY_MESSAGE)
        try:
            return int(answer[b"UIDVALIDITY"])
        except (KeyError, TypeError, ValueError):
            raise MailServerError(INCOMPLETE_MESSAGE) from None

    def search(self, criteria: Sequence[Any], charset: str | None = None) -> list[int]:
        """UIDs in the open mailbox matching ``criteria``: keys, dates and :class:`Term` values."""
        wire = _wire_criteria(criteria)
        with _translated():
            return [int(uid) for uid in self._client.search(wire, charset)]

    def fetch(self, uids: Iterable[int], items: Sequence[str]) -> dict[int, dict[bytes, Any]]:
        """Fetch ``items`` for ``uids`` from the open mailbox, peeking only."""
        for item in items:
            if item not in _PLAIN_ITEMS and not _PEEK_ITEM.fullmatch(item):
                raise ValueError(f"not a read-only fetch item: {item!r}")
        result: dict[int, dict[bytes, Any]] = {}
        with _translated():
            for chunk in batches(uids):
                result.update(self._client.fetch(chunk, list(items)))
        return result

    def close(self) -> None:
        """Sign out. A connection that is already gone is not an error."""
        try:
            self._client.logout()
        except Exception:  # noqa: BLE001 - nothing useful to say about a failed goodbye
            # But the socket must not wait for the garbage collector.
            with suppress(Exception):
                self._client.shutdown()
