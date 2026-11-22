"""Reading a message's structure and headers without downloading the message.

A message can be tens of megabytes of attachments around a few kilobytes of
text. IMAP's BODYSTRUCTURE describes every part, so the server is asked for the
text parts a reader would see and for nothing else; attachments are listed from
the structure by name, type and size and never fetched.

Everything here is written by whoever sent the mail, so it is parsed with the
standard library's simple, linear-time helpers and bounded before it is parsed:
the structured header parser (``email.policy.default``) takes time quadratic in
the length of an address header, which a sender chooses.
"""

import base64
import binascii
import codecs
import hashlib
import quopri
import re
from collections.abc import Sequence
from dataclasses import dataclass
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesHeaderParser
from email.utils import collapse_rfc2231_value, decode_rfc2231, getaddresses
from typing import Any
from urllib.parse import unquote

from second_brain_core.textclean import clean_line

# The header fields fetched for a message that is listed or read.
HEADER_FIELDS = ("DATE", "FROM", "TO", "CC", "SUBJECT", "MESSAGE-ID", "X-PM-INTERNAL-ID")
# The ones that identify a message (key_of).
KEY_FIELDS = ("DATE", "FROM", "SUBJECT", "MESSAGE-ID", "X-PM-INTERNAL-ID")
MAX_SUBJECT_CHARS = 300
MAX_ADDRESS_CHARS = 200
MAX_NAME_CHARS = 200
# Recipients beyond this many are counted, not listed.
MAX_RECIPIENTS = 20
# How much of one header's value is looked at. A real one is far shorter.
MAX_HEADER_CHARS = 16_000
# How many text parts of one message are read as its body.
MAX_BODY_PARTS = 8
# What a content type's two halves may look like (RFC 6838, bounded).
_TYPE_TOKEN = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}")
# RFC 2231: a long or non-ASCII parameter split into numbered pieces, `name*0*`, `name*1`.
_CONTINUED = re.compile(r"(filename|name)\*(\d{1,3})(\*?)")
# Character sets mail is really written in. A sender names the charset, and Python
# has codecs that are not character sets at all (unicode_escape, rot13, zlib).
_CHARSET_FAMILIES = (
    "ascii", "big5", "cp", "euc", "gb", "hz", "iso2022", "iso8859", "johab", "koi8", "kz1048",
    "latin", "mac", "ptcp154", "shift_jis", "tis_620", "utf_16", "utf_32", "utf_8",
)  # fmt: skip
_NOT_CHARSETS = frozenset({"cp65001"})


@dataclass(frozen=True)
class Part:
    """One leaf of a message's MIME structure."""

    # The IMAP section number: "1", "2.1". What BODY.PEEK[...] is asked for.
    section: str
    content_type: str
    charset: str
    encoding: str
    # Size as stored, in bytes: of the encoded form for base64 and quoted-printable.
    size: int
    disposition: str
    filename: str

    @property
    def is_text_body(self) -> bool:
        """A text part shown as the message, not offered as a file."""
        return (
            self.content_type in ("text/plain", "text/html")
            and self.disposition != "attachment"
            and not self.filename
        )

    @property
    def decoded_size(self) -> int:
        """Roughly the size once decoded (base64 stores three bytes in four)."""
        return self.size * 3 // 4 if self.encoding == "base64" else self.size


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def _params(value: Any) -> dict[str, str]:
    """A parameter list ``(key, value, key, value, ...)`` as a dict with lowercase keys."""
    if not isinstance(value, (tuple, list)):
        return {}
    items = list(value)
    return {_text(k).lower(): _text(v) for k, v in zip(items[0::2], items[1::2], strict=False)}


def _decode_words(text: str) -> str:
    """``text`` with its RFC 2047 encoded words (``=?utf-8?B?...?=``) decoded."""
    try:
        return str(make_header(decode_header(text)))
    except Exception:  # noqa: BLE001 - a malformed encoded word stays as it was written
        return text


def _extended(raw: str) -> str:
    """An RFC 2231 extended value: ``charset'language'percent-encoded``."""
    charset, language, encoded = decode_rfc2231(raw)
    return collapse_rfc2231_value((charset, language, unquote(encoded, encoding="latin-1")))


def _filename(*param_sets: dict[str, str]) -> str:
    """The part's file name from its disposition or content-type parameters, decoded."""
    for params in param_sets:
        try:
            pieces = sorted(
                (int(match.group(2)), match.group(3), params[key])
                for key in params
                if (match := _CONTINUED.fullmatch(key))
            )
            if pieces:
                # Only the first piece carries the charset; the rest are percent-encoded
                # when marked with a star and literal otherwise.
                first = pieces[0]
                name = _extended(first[2]) if first[1] else first[2]
                charset = decode_rfc2231(first[2])[0] if first[1] else None
                for _, star, value in pieces[1:]:
                    name += (
                        unquote(value, encoding=charset or "utf-8", errors="replace")
                        if star
                        else value
                    )
                return clean_line(name, MAX_NAME_CHARS)
            for key in ("filename*", "name*", "filename", "name"):
                raw = params.get(key)
                if raw:
                    name = _extended(raw) if key.endswith("*") else _decode_words(raw)
                    return clean_line(name, MAX_NAME_CHARS)
        except Exception:  # noqa: BLE001 - a malformed name is still a name
            for key in ("filename", "name", "filename*", "name*"):
                if params.get(key):
                    return clean_line(params[key], MAX_NAME_CHARS)
    return ""


def _type_token(value: Any) -> str:
    """One half of a content type, or ``x-unknown``: the sender writes it, so it is not trusted."""
    token = _text(value).lower()
    return token if _TYPE_TOKEN.fullmatch(token) else "x-unknown"


def _is_multipart(node: Any) -> bool:
    return isinstance(node, (tuple, list)) and bool(node) and isinstance(node[0], list)


def _leaf(node: Sequence[Any], section: str) -> Part:
    main = _type_token(node[0])
    sub = _type_token(node[1]) if len(node) > 1 else "x-unknown"
    params = _params(node[2]) if len(node) > 2 else {}
    encoding = _text(node[5]).lower() if len(node) > 5 else ""
    size = node[6] if len(node) > 6 and isinstance(node[6], int) else 0
    # After the size come fields that depend on the type (a line count for text;
    # an envelope, a structure and a line count for an attached message), then
    # the MD5 and then the disposition: ``(b"attachment", (b"filename", ...))``.
    # Found by shape, so one field more or fewer doesn't misplace it: a pair of a
    # keyword and a parameter list (or nothing). A language list, the next field,
    # is a pair of two strings and is not mistaken for it.
    disposition, disposition_params = "", {}
    for field in node[7:]:
        if (
            isinstance(field, tuple)
            and len(field) == 2
            and isinstance(field[0], bytes)
            and field[0].lower() in (b"attachment", b"inline")
            and (field[1] is None or isinstance(field[1], (tuple, list)))
        ):
            disposition = field[0].decode().lower()
            disposition_params = _params(field[1])
            break
    return Part(
        section=section,
        content_type=f"{main}/{sub}",
        charset=params.get("charset", ""),
        encoding=encoding,
        size=size,
        disposition=disposition,
        filename=_filename(disposition_params, params),
    )


def flatten(structure: Any, prefix: str = "") -> list[Part]:
    """The leaves of a BODYSTRUCTURE as IMAPClient parses it, in order, numbered.

    A multipart node is ``([children], subtype, ...)``; its children are numbered
    ``1``, ``2``, ... under the parent's own number. A message that isn't
    multipart is its own part ``1``. An attached message (``message/rfc822``) is
    a leaf: it is somebody else's mail and is listed as an attachment, not read.
    """
    if not isinstance(structure, (tuple, list)) or not structure:
        return []
    if _is_multipart(structure):
        parts: list[Part] = []
        for index, child in enumerate(structure[0], start=1):
            parts.extend(flatten(child, f"{prefix}{index}."))
        return parts
    return [_leaf(structure, prefix.rstrip(".") or "1")]


def _bodies(structure: Any, prefix: str) -> list[Part]:
    if not isinstance(structure, (tuple, list)) or not structure:
        return []
    if not _is_multipart(structure):
        part = _leaf(structure, prefix.rstrip(".") or "1")
        return [part] if part.is_text_body and part.size > 0 else []
    children = [
        _bodies(child, f"{prefix}{index}.") for index, child in enumerate(structure[0], start=1)
    ]
    subtype = _text(structure[1]).lower() if len(structure) > 1 else ""
    if subtype == "alternative":
        # The same message in several forms. A mail client shows the richest it can,
        # which is the HTML: so that is the one a person has seen. The plain form
        # is used only when there is no HTML.
        with_html = [
            found for found in children if any(p.content_type == "text/html" for p in found)
        ]
        return with_html[-1] if with_html else next((found for found in children if found), [])
    return [part for found in children for part in found]


def choose_bodies(structure: Any) -> list[Part]:
    """The text parts a reader's mail client would show as the message, in order.

    Of the alternative forms of a message, the HTML one. Otherwise every text
    part that is shown and not attached: a body, and a footer a mailing list
    added after it. At most :data:`MAX_BODY_PARTS`.
    """
    return _bodies(structure, "")[:MAX_BODY_PARTS]


def attachments_of(parts: list[Part], bodies: Sequence[Part] = ()) -> list[Part]:
    """Every part offered as a file: named, or marked as an attachment."""
    shown = {part.section for part in bodies}
    return [
        part
        for part in parts
        if part.section not in shown and (part.filename or part.disposition == "attachment")
    ]


def _charset(name: str) -> str:
    """The codec for a declared charset when it is a real character set, else UTF-8."""
    try:
        codec = codecs.lookup(name.strip()).name.replace("-", "_")
    except (LookupError, ValueError, TypeError):
        return "utf-8"
    if codec in _NOT_CHARSETS or not codec.startswith(_CHARSET_FAMILIES):
        return "utf-8"
    return codec


def decode_body(raw: bytes, part: Part) -> str:
    """The text of ``part`` from its stored bytes, which may have been cut short."""
    if part.encoding == "base64":
        packed = b"".join(raw.split())
        try:
            data = base64.b64decode(packed[: len(packed) - len(packed) % 4])
        except (binascii.Error, ValueError):
            data = b""
    elif part.encoding == "quoted-printable":
        data = quopri.decodestring(raw)
    else:
        data = raw
    try:
        return data.decode(_charset(part.charset or "utf-8"), errors="replace")
    except (LookupError, ValueError):
        return data.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class Headers:
    """The header fields shown for a message, decoded and cleaned."""

    sender: str
    to: list[str]
    cc: list[str]
    # How many more recipients there are than ``to`` and ``cc`` list.
    more_recipients: int
    subject: str


def _repair(text: str) -> str:
    """``text`` with bytes the header parser couldn't place read as UTF-8.

    A header may hold raw 8-bit text where the standard asks for encoded words
    (Bridge writes some that way). The parser keeps each such byte as a lone
    surrogate, which is not text and can't be sent to a client; read back as
    UTF-8 they are the characters that were meant.
    """
    return text.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")


def _raw_headers(raw: bytes) -> Message:
    """The header block parsed only as far as names and raw values."""
    return BytesHeaderParser(policy=policy.compat32).parsebytes(raw)


def _value(message: Message, name: str) -> str:
    """The first header called ``name`` as text, bounded; empty when there is none.

    Read from the raw items: asked for by name, a value holding 8-bit bytes comes
    back as a ``Header`` object that has already replaced them.
    """
    wanted = name.lower()
    for field, value in message.raw_items():
        if field.lower() == wanted:
            return _repair(str(value))[:MAX_HEADER_CHARS]
    return ""


def _addresses(message: Message, name: str) -> tuple[list[str], int]:
    """The first :data:`MAX_RECIPIENTS` addresses of a header, and how many more there are."""
    value = _value(message, name)
    if not value.strip():
        return [], 0
    try:
        pairs = [(n, a) for n, a in getaddresses([value]) if n or a]
    except Exception:  # noqa: BLE001 - a header the parser gives up on is shown as written
        pairs = []
    if not any("@" in address for _, address in pairs):
        shown = clean_line(_decode_words(value), MAX_ADDRESS_CHARS)
        return ([shown] if shown else []), 0
    result = []
    for display, address in pairs[:MAX_RECIPIENTS]:
        display = _decode_words(display)
        shown = f"{display} <{address}>" if display and address else display or address
        result.append(clean_line(shown, MAX_ADDRESS_CHARS))
    return [entry for entry in result if entry], max(len(pairs) - MAX_RECIPIENTS, 0)


def key_of(raw: bytes) -> str:
    """What identifies a message in every mailbox that holds a copy of it.

    Proton Mail Bridge stamps each message with Proton's own id, which is the
    same in a folder, in every label and in All Mail. Elsewhere it is a digest of
    the Message-ID together with the date, sender and subject, all of which a
    copy shares with the original. (The Message-ID alone would let anyone who
    knows a message's id send another that passes for its copy.)

    Cheap on purpose: it runs on every message of a hidden mailbox and on every
    candidate of a search, so nothing here parses an address.
    """
    message = _raw_headers(raw)
    proton = _value(message, "X-Pm-Internal-Id").strip()
    if proton:
        return f"pm:{proton}"
    basis = "\x00".join(
        _value(message, name).strip() for name in ("Message-ID", "Date", "From", "Subject")
    )
    return "fp:" + hashlib.sha256(basis.encode("utf-8", errors="replace")).hexdigest()


def parse_headers(raw: bytes) -> Headers:
    """Decode the fetched header fields (encoded words, address lists) and clean them."""
    message = _raw_headers(raw)
    senders, _ = _addresses(message, "From")
    to, more_to = _addresses(message, "To")
    cc, more_cc = _addresses(message, "Cc")
    return Headers(
        sender=senders[0] if senders else "",
        to=to,
        cc=cc,
        more_recipients=more_to + more_cc,
        subject=clean_line(_decode_words(_value(message, "Subject")), MAX_SUBJECT_CHARS),
    )
