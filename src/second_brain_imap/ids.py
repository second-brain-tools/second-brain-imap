"""The ids this server hands out for messages.

An id names one copy of a message: the mailbox it was found in, that mailbox's
UIDVALIDITY and the message's UID there. That is what IMAP can fetch directly,
with no search. It stays good until the mail server rebuilds the mailbox (a new
UIDVALIDITY), and then it is refused as out of date, never matched to whatever
message has that number now.

It is packed as URL-safe base64 so that a client passes it back unchanged; it is
not a secret and holds nothing the client wasn't already shown.
"""

import base64
import binascii
import json
import re
from dataclasses import dataclass

from .errors import MailNotFoundError

# Long enough for the longest mailbox name allowed below, in any script.
ID_PATTERN = r"^[A-Za-z0-9_-]{8,4096}$"
_ID = re.compile(ID_PATTERN)
MAX_FOLDER_CHARS = 500
_MAX_NUMBER = 2**32 - 1


@dataclass(frozen=True)
class MessageRef:
    """Where one copy of a message is."""

    folder: str
    uidvalidity: int
    uid: int


def encode(ref: MessageRef) -> str:
    """The id for ``ref``."""
    packed = json.dumps([ref.folder, ref.uidvalidity, ref.uid], separators=(",", ":"))
    return base64.urlsafe_b64encode(packed.encode()).decode().rstrip("=")


def _is_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= _MAX_NUMBER


def decode(value: str) -> MessageRef:
    """The reference in an id, or :class:`MailNotFoundError` for anything else.

    An id that was never issued by this server gets the same answer as a message
    that doesn't exist.
    """
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise MailNotFoundError
    try:
        fields = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (binascii.Error, ValueError, RecursionError):  # deeply nested JSON recurses
        raise MailNotFoundError from None
    if (
        not isinstance(fields, list)
        or len(fields) != 3
        or not isinstance(fields[0], str)
        or not 0 < len(fields[0]) <= MAX_FOLDER_CHARS
        or not _is_number(fields[1])
        or not _is_number(fields[2])
    ):
        raise MailNotFoundError
    return MessageRef(folder=fields[0], uidvalidity=fields[1], uid=fields[2])
