"""Message ids: what is issued comes back; anything else is "not found"."""

import base64
import json

import pytest

from second_brain_imap import ids
from second_brain_imap.errors import NOT_FOUND_MESSAGE, MailNotFoundError


@pytest.mark.parametrize(
    "folder",
    ["INBOX", "Folders/Receipts 2026", "Étiquettes/À faire", "a" * 500, "日本語のフォルダ" * 62],
)
def test_round_trip(folder: str) -> None:
    ref = ids.MessageRef(folder, 119887746, 833)
    packed = ids.encode(ref)
    assert ids.decode(packed) == ref
    assert "=" not in packed and "/" not in packed and "+" not in packed


def pack(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "short",
        "has spaces in it!",
        "A" * 5000,
        "AAAAAAAAAAAA",  # base64, not JSON
        pack({"folder": "INBOX"}),
        pack(["INBOX", 1]),
        pack(["INBOX", 1, 2, 3]),
        pack([42, 1, 2]),
        pack(["", 1, 2]),
        pack(["a" * 501, 1, 2]),
        pack(["INBOX", "1", 2]),
        pack(["INBOX", 1, 2.5]),
        pack(["INBOX", True, 2]),
        pack(["INBOX", 0, 2]),
        pack(["INBOX", 1, -2]),
        pack(["INBOX", 1, 2**32]),
        pack("just a string"),
    ],
)
def test_anything_else_is_not_found(bad: str) -> None:
    with pytest.raises(MailNotFoundError) as caught:
        ids.decode(bad)
    assert str(caught.value) == NOT_FOUND_MESSAGE


def test_a_non_string_is_not_found() -> None:
    with pytest.raises(MailNotFoundError):
        ids.decode(None)  # type: ignore[arg-type]


def test_an_id_made_to_exhaust_the_parser_is_just_not_found() -> None:
    deep = base64.urlsafe_b64encode(b"[" * 3000).decode().rstrip("=")
    with pytest.raises(MailNotFoundError):
        ids.decode(deep)
