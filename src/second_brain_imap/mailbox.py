"""Listing, searching and reading the mailbox, under the folder filter.

Every public method opens its own connection, does its work and signs out, so
nothing depends on a connection surviving between calls (the mail server can be
restarted at any time) and two calls never share a selected mailbox. Everything
here is synchronous; the tools run it in a worker thread.

Two rules hold for every message that leaves this module:

- it was found in a mailbox the filter lets a client open, and
- it is not also in a hidden mailbox (scope.py): a hidden message is withheld
  wherever the mail server shows another copy of it.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from . import ids, imap, mime
from .config import Settings
from .errors import STALE_ID_MESSAGE, MailError, MailNotFoundError, MailServerError
from .imap import (
    INCOMPLETE_MESSAGE,
    FolderStatus,
    ImapClient,
    ReadOnlySession,
    Term,
    section_value,
)
from .models import (
    MAX_TERM_CHARS,
    Attachment,
    Folder,
    Message,
    MessageSummary,
    SearchResults,
)
from .scope import VIEW_ROLES, FolderInfo, FolderScope, scope_of
from .textclean import clean_line, html_to_text, tidy

# The newest matches a search looks at. A broader search reports ``more``.
MAX_CONSIDERED = 2000
# The most of a message's text part that is loaded, in bytes as stored.
MAX_BODY_BYTES = 4 * 1024 * 1024
# Recipients shown per message in a list of results.
SUMMARY_RECIPIENTS = 5

_HEADERS_ITEM = f"BODY.PEEK[HEADER.FIELDS ({' '.join(mime.HEADER_FIELDS)})]"
# The fields that identify a message, for the many messages that are only compared.
_KEYS_ITEM = f"BODY.PEEK[HEADER.FIELDS ({' '.join(mime.KEY_FIELDS)})]"
_HEADERS_PREFIX = "BODY[HEADER.FIELDS"
_SYSTEM_ROLES = ("inbox", "sent", "drafts", "archive")

Connect = Callable[[Settings], ImapClient]


@dataclass(frozen=True)
class SearchQuery:
    """What to look for. Every field narrows the search; none set lists the newest."""

    text: str = ""
    sender: str = ""
    recipient: str = ""
    subject: str = ""
    since: date | None = None
    before: date | None = None
    folder: str = ""
    unread_only: bool = False
    limit: int = 20


@dataclass(frozen=True)
class _Hit:
    when: datetime
    order: int
    folder: str
    uidvalidity: int
    uid: int


@dataclass(frozen=True)
class _Loaded:
    headers: mime.Headers
    flags: frozenset[str]


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _flags(value: Any) -> frozenset[str]:
    if not isinstance(value, (tuple, list)):
        return frozenset()
    return frozenset(
        (flag.decode("ascii", errors="replace") if isinstance(flag, bytes) else str(flag)).lower()
        for flag in value
    )


def _term(value: str) -> Term | None:
    """A search term as one line with no control characters, at a bounded length."""
    cleaned = clean_line(value, MAX_TERM_CHARS)
    return Term(cleaned) if cleaned else None


def criteria_of(query: SearchQuery) -> tuple[list[Any], str | None]:
    """IMAP SEARCH criteria for ``query``, and the charset they need."""
    criteria: list[Any] = []
    for key, value in (("FROM", query.sender), ("SUBJECT", query.subject), ("TEXT", query.text)):
        term = _term(value)
        if term:
            criteria += [key, term]
    recipient = _term(query.recipient)
    if recipient:
        criteria += ["OR", "TO", recipient, "CC", recipient]
    if query.since is not None:
        criteria += ["SINCE", query.since]
    if query.before is not None:
        criteria += ["BEFORE", query.before]
    if query.unread_only:
        criteria.append("UNSEEN")
    if not criteria:
        criteria = ["ALL"]
    terms = (query.sender, query.subject, query.text, query.recipient)
    charset = None if all(term.isascii() for term in terms) else "UTF-8"
    return criteria, charset


def _ordered(folders: list[FolderInfo]) -> list[FolderInfo]:
    """Inbox, Sent, Drafts and Archive first, then the rest by name."""

    def rank(folder: FolderInfo) -> tuple[int, str]:
        role = folder.role
        return (_SYSTEM_ROLES.index(role) if role in _SYSTEM_ROLES else len(_SYSTEM_ROLES)), (
            folder.name.casefold()
        )

    return sorted(folders, key=rank)


def find_folder(name: str, folders: list[FolderInfo]) -> FolderInfo | None:
    """The mailbox called ``name``: exactly, except that INBOX is case-insensitive."""
    for folder in folders:
        if folder.name == name or (name.upper() == "INBOX" and folder.name.upper() == "INBOX"):
            return folder
    return None


class Mailbox:
    """The mailbox as the tools see it."""

    def __init__(self, settings: Settings, connect: Connect | None = None) -> None:
        self._settings = settings
        self._connect = connect if connect is not None else imap.connect
        # Per hidden mailbox: the state it was read in, and the keys of its messages.
        self._hidden_cache: dict[str, tuple[tuple[int, int, int], frozenset[str]]] = {}
        self._lock = threading.Lock()

    @contextmanager
    def _session(self) -> Iterator[ReadOnlySession]:
        session = ReadOnlySession(self._connect(self._settings))
        try:
            yield session
        finally:
            session.close()

    # ── what a client may see ────────────────────────────────────────────────────────────────────

    def _resolve(self, session: ReadOnlySession) -> tuple[list[FolderInfo], FolderScope]:
        folders = session.folders()
        return folders, scope_of(self._settings, folders)

    def _hidden_keys(self, session: ReadOnlySession, scope: FolderScope) -> frozenset[str]:
        """The keys of every message in a hidden mailbox.

        Read again only when the mailbox has changed: its UIDVALIDITY, next UID
        and message count are compared with those it was last read in. Callers
        ask for this AFTER they have collected the messages they mean to show:
        a message that lands in Spam while a call is running is then either in
        this set or not among the candidates, never between the two.
        """
        keys: set[str] = set()
        for name in sorted(scope.hidden):
            status = session.status(name)
            stamp = (status.uidvalidity, status.uidnext, status.messages)
            with self._lock:
                cached = self._hidden_cache.get(name)
            if cached is not None and cached[0] == stamp:
                keys |= cached[1]
                continue
            found: set[str] = set()
            if status.messages:
                session.examine(name)
                uids = session.search(["ALL"])
                fetched = session.fetch(uids, [_KEYS_ITEM])
                for uid in uids:
                    raw = section_value(fetched.get(uid, {}), _HEADERS_PREFIX)
                    if raw is None:
                        # A hidden message that can't be identified can't be withheld
                        # elsewhere. Refuse; never carry on with part of the set.
                        raise MailServerError(INCOMPLETE_MESSAGE)
                    found.add(mime.key_of(raw))
            with self._lock:
                self._hidden_cache[name] = (stamp, frozenset(found))
            keys |= found
        return frozenset(keys)

    def _targets(self, wanted: str, folders: list[FolderInfo], scope: FolderScope) -> list[str]:
        """The mailboxes a search runs in.

        A named mailbox, when one is asked for. Otherwise the server's
        all-mail view if the client may open it (one search, each message once),
        and failing that every visible mailbox that is not such a view.
        """
        if wanted:
            folder = find_folder(wanted, folders)
            if folder is None or not scope.allows(folder.name):
                raise MailNotFoundError
            return [folder.name]
        visible = _ordered([folder for folder in folders if scope.allows(folder.name)])
        for folder in visible:
            if folder.role == "all":
                return [folder.name]
        real = [folder.name for folder in visible if folder.role not in VIEW_ROLES]
        return real or [folder.name for folder in visible]

    # ── operations ───────────────────────────────────────────────────────────────────────────────

    def ping(self) -> bool:
        """True when the mail server takes the sign-in."""
        try:
            with self._session():
                return True
        except MailError:
            return False

    def inventory(self) -> list[tuple[FolderInfo, FolderStatus | None]]:
        """Every mailbox with its counts, filter or no filter: the operator's own view."""
        with self._session() as session:
            return [
                (folder, session.status(folder.name) if folder.selectable else None)
                for folder in _ordered(session.folders())
            ]

    def list_folders(self) -> list[Folder]:
        """The mailboxes a client may open, with their message counts."""
        with self._session() as session:
            folders, scope = self._resolve(session)
            result = []
            for folder in _ordered(folders):
                if not scope.allows(folder.name):
                    continue
                status = session.status(folder.name)
                result.append(
                    Folder(
                        name=folder.name,
                        role=folder.role,
                        messages=status.messages,
                        unread=status.unseen,
                    )
                )
            return result

    @staticmethod
    def _by_folder(hits: list[_Hit]) -> dict[str, list[_Hit]]:
        grouped: dict[str, list[_Hit]] = {}
        for hit in hits:
            grouped.setdefault(hit.folder, []).append(hit)
        return grouped

    def _keys(self, session: ReadOnlySession, hits: list[_Hit]) -> dict[_Hit, str]:
        """What identifies each of ``hits`` (mime.key_of), one mailbox at a time."""
        keys: dict[_Hit, str] = {}
        for folder, group in self._by_folder(hits).items():
            session.examine(folder)
            fetched = session.fetch([hit.uid for hit in group], [_KEYS_ITEM])
            for hit in group:
                raw = section_value(fetched.get(hit.uid, {}), _HEADERS_PREFIX)
                if raw is not None:  # else: gone between the search and the fetch
                    keys[hit] = mime.key_of(raw)
        return keys

    def _details(self, session: ReadOnlySession, hits: list[_Hit]) -> dict[_Hit, _Loaded]:
        """Headers and flags for the messages that are going to be shown."""
        loaded: dict[_Hit, _Loaded] = {}
        for folder, group in self._by_folder(hits).items():
            session.examine(folder)
            fetched = session.fetch([hit.uid for hit in group], ["FLAGS", _HEADERS_ITEM])
            for hit in group:
                items = fetched.get(hit.uid)
                raw = section_value(items, _HEADERS_PREFIX) if items else None
                if items is None or raw is None:
                    continue  # gone between the search and the fetch
                loaded[hit] = _Loaded(mime.parse_headers(raw), _flags(items.get(b"FLAGS")))
        return loaded

    def search(self, query: SearchQuery) -> SearchResults:
        """Messages matching ``query``, newest first."""
        criteria, charset = criteria_of(query)
        with self._session() as session:
            folders, scope = self._resolve(session)
            targets = self._targets(query.folder, folders, scope)
            hits: list[_Hit] = []
            for order, folder in enumerate(targets):
                uidvalidity = session.examine(folder)
                uids = session.search(criteria, charset)
                if not uids:
                    continue
                dates = session.fetch(uids, ["INTERNALDATE"])
                for uid in uids:
                    when = _aware(dates.get(uid, {}).get(b"INTERNALDATE"))
                    if when is not None:
                        hits.append(_Hit(when, order, folder, uidvalidity, uid))
            # Newest first; of two copies with the same time, the earlier mailbox's.
            hits.sort(key=lambda hit: (-hit.when.timestamp(), hit.order, -hit.uid))
            capped = len(hits) > MAX_CONSIDERED
            hits = hits[:MAX_CONSIDERED]
            if scope.hidden or len(targets) > 1:
                # Every candidate's key is needed: to drop hidden messages, and the
                # second copy of one that is in two of the searched mailboxes. The
                # candidates' keys first, the hidden ones after (see _hidden_keys).
                keys = self._keys(session, hits)
                hidden = self._hidden_keys(session, scope)
                first_seen: dict[str, str] = {}
                kept = []
                for hit in hits:
                    key = keys.get(hit)
                    if key is None or key in hidden:
                        continue
                    # A second message with the same key in the SAME mailbox is not a
                    # copy, whatever its headers claim: both are shown.
                    if first_seen.setdefault(key, hit.folder) != hit.folder:
                        continue
                    kept.append(hit)
            else:
                kept = hits
            loaded = self._details(session, kept[: query.limit])
        page = [hit for hit in kept[: query.limit] if hit in loaded]
        return SearchResults(
            total=len(kept),
            more=capped or len(kept) > len(page),
            messages=[self._summary(hit, loaded[hit]) for hit in page],
        )

    @staticmethod
    def _summary(hit: _Hit, loaded: _Loaded) -> MessageSummary:
        return MessageSummary(
            id=ids.encode(ids.MessageRef(hit.folder, hit.uidvalidity, hit.uid)),
            date=_iso(hit.when),
            sender=loaded.headers.sender,
            to=loaded.headers.to[:SUMMARY_RECIPIENTS],
            subject=loaded.headers.subject,
            folder=hit.folder,
            unread="\\seen" not in loaded.flags,
            starred="\\flagged" in loaded.flags,
        )

    def read(self, message_id: str, offset: int, max_chars: int) -> Message:
        """One message by the id a search gave, with ``max_chars`` of its text from ``offset``."""
        ref = ids.decode(message_id)
        with self._session() as session:
            folders, scope = self._resolve(session)
            folder = find_folder(ref.folder, folders)
            if folder is None or folder.name != ref.folder or not scope.allows(folder.name):
                raise MailNotFoundError
            if session.examine(folder.name) != ref.uidvalidity:
                raise MailNotFoundError(STALE_ID_MESSAGE)
            fetched = session.fetch(
                [ref.uid], ["FLAGS", "INTERNALDATE", "BODYSTRUCTURE", _HEADERS_ITEM]
            )
            items = fetched.get(ref.uid)
            raw_headers = section_value(items, _HEADERS_PREFIX) if items else None
            when = _aware(items.get(b"INTERNALDATE")) if items else None
            if items is None or raw_headers is None or when is None:
                raise MailNotFoundError
            structure = items.get(b"BODYSTRUCTURE")
            bodies = mime.choose_bodies(structure)
            # Each text part up to what is left of the budget, in one request.
            wanted: list[tuple[mime.Part, int]] = []
            budget = MAX_BODY_BYTES
            for part in bodies:
                take = min(part.size, budget)
                if take > 0:
                    wanted.append((part, take))
                    budget -= take
            texts: list[str] = []
            if wanted:
                answer = session.fetch(
                    [ref.uid],
                    [f"BODY.PEEK[{part.section}]<0.{take}>" for part, take in wanted],
                ).get(ref.uid, {})
                for part, _ in wanted:
                    raw = section_value(answer, f"BODY[{part.section}]") or b""
                    decoded = mime.decode_body(raw, part)
                    texts.append(
                        html_to_text(decoded) if part.content_type == "text/html" else tidy(decoded)
                    )
            # The message is in hand; only now is it compared with what is hidden
            # (see _hidden_keys), and the mailbox it came from is opened again after.
            if mime.key_of(raw_headers) in self._hidden_keys(session, scope):
                raise MailNotFoundError
        headers = mime.parse_headers(raw_headers)
        flags = _flags(items.get(b"FLAGS"))
        text = "\n\n".join(piece for piece in texts if piece)
        window = text[offset : offset + max_chars]
        end = offset + len(window)
        return Message(
            id=message_id,
            date=_iso(when),
            sender=headers.sender,
            to=headers.to,
            cc=headers.cc,
            more_recipients=headers.more_recipients,
            subject=headers.subject,
            folder=folder.name,
            unread="\\seen" not in flags,
            starred="\\flagged" in flags,
            attachments=[
                Attachment(
                    name=part.filename or "(unnamed)",
                    type=part.content_type,
                    size_bytes=part.decoded_size,
                )
                for part in mime.attachments_of(mime.flatten(structure), bodies)
            ],
            body=window,
            offset=offset,
            total_chars=len(text),
            next_offset=end if end < len(text) else None,
            body_truncated=sum(take for _, take in wanted) < sum(part.size for part in bodies),
        )
