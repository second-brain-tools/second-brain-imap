"""The MCP tools: list the mailboxes, search, list recent mail, read one message."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..context import app_context
from ..errors import FolderFilterError, MailError
from ..ids import ID_PATTERN, MAX_FOLDER_CHARS
from ..mailbox import SearchQuery
from ..models import (
    DATE_PATTERN,
    DEFAULT_RECENT_DAYS,
    DEFAULT_SEARCH_LIMIT,
    MAX_RECENT_DAYS,
    MAX_SEARCH_LIMIT,
    MAX_TERM_CHARS,
    READ_DEFAULT_CHARS,
    READ_MAX_CHARS,
    Folder,
    Message,
    SearchResults,
)
from .annotations import READ_ONLY
from .failure import tool_error, unexpected

Term = Annotated[str, Field(max_length=MAX_TERM_CHARS)]
FolderName = Annotated[str, Field(max_length=MAX_FOLDER_CHARS)]
OneFolder = Annotated[str, Field(min_length=1, max_length=MAX_FOLDER_CHARS)]
IsoDate = Annotated[str, Field(pattern=DATE_PATTERN, description="A date as YYYY-MM-DD.")]
SearchLimit = Annotated[int, Field(ge=1, le=MAX_SEARCH_LIMIT)]
Days = Annotated[int, Field(ge=1, le=MAX_RECENT_DAYS)]
MessageId = Annotated[
    str, Field(pattern=ID_PATTERN, description="A message's id, from mail_search or mail_recent.")
]
Offset = Annotated[int, Field(ge=0)]
MaxChars = Annotated[int, Field(ge=1, le=READ_MAX_CHARS)]


def _day(value: str | None, name: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{name} is not a date: use YYYY-MM-DD") from None


def register(mcp: FastMCP) -> None:
    """Register the tools on the given FastMCP server."""

    @mcp.tool(annotations=READ_ONLY)
    async def mail_list_folders(ctx: Context) -> list[Folder]:
        """List the mailboxes (folders and labels) with their message and unread counts."""
        app = app_context(ctx)
        try:
            return await asyncio.to_thread(app.mailbox.list_folders)
        except (MailError, FolderFilterError) as exc:
            raise tool_error(exc) from exc
        except Exception as exc:  # noqa: BLE001 - see failure.unexpected
            raise unexpected("mail_list_folders", exc) from None

    @mcp.tool(annotations=READ_ONLY)
    async def mail_search(
        ctx: Context,
        text: Term = "",
        sender: Term = "",
        recipient: Term = "",
        subject: Term = "",
        since: IsoDate | None = None,
        before: IsoDate | None = None,
        folder: FolderName = "",
        unread_only: bool = False,
        limit: SearchLimit = DEFAULT_SEARCH_LIMIT,
    ) -> SearchResults:
        """Search mail, newest first. Every argument given must match.

        `text` looks in the whole message, `sender` in From, `recipient` in To and
        Cc, `subject` in the subject; each matches a word or phrase exactly as
        written, in any letter case. `since` is the first day included and `before`
        the first day left out. `folder` is a name from mail_list_folders; without
        it the whole mailbox is searched. Read a result with mail_read and its id.
        """
        app = app_context(ctx)
        query = SearchQuery(
            text=text,
            sender=sender,
            recipient=recipient,
            subject=subject,
            since=_day(since, "since"),
            before=_day(before, "before"),
            folder=folder,
            unread_only=unread_only,
            limit=limit,
        )
        try:
            return await asyncio.to_thread(app.mailbox.search, query)
        except (MailError, FolderFilterError) as exc:
            raise tool_error(exc) from exc
        except Exception as exc:  # noqa: BLE001 - see failure.unexpected
            raise unexpected("mail_search", exc) from None

    @mcp.tool(annotations=READ_ONLY)
    async def mail_recent(
        ctx: Context,
        folder: OneFolder = "INBOX",
        days: Days = DEFAULT_RECENT_DAYS,
        unread_only: bool = False,
        limit: SearchLimit = DEFAULT_SEARCH_LIMIT,
    ) -> SearchResults:
        """List the mail that arrived in the last `days` days in one mailbox, newest first."""
        app = app_context(ctx)
        query = SearchQuery(
            since=datetime.now(UTC).date() - timedelta(days=days),
            folder=folder,
            unread_only=unread_only,
            limit=limit,
        )
        try:
            return await asyncio.to_thread(app.mailbox.search, query)
        except (MailError, FolderFilterError) as exc:
            raise tool_error(exc) from exc
        except Exception as exc:  # noqa: BLE001 - see failure.unexpected
            raise unexpected("mail_recent", exc) from None

    @mcp.tool(annotations=READ_ONLY)
    async def mail_read(
        ctx: Context,
        message_id: MessageId,
        offset: Offset = 0,
        max_chars: MaxChars = READ_DEFAULT_CHARS,
    ) -> Message:
        """Read one message as plain text, a page at a time for long ones.

        Returns up to `max_chars` characters of the text starting at `offset`. When
        `next_offset` is set, call again with `offset=next_offset` for the rest.
        Attachments are listed by name, type and size; their content is not
        available. Reading does not mark the message as read.
        """
        app = app_context(ctx)
        try:
            return await asyncio.to_thread(app.mailbox.read, message_id, offset, max_chars)
        except (MailError, FolderFilterError) as exc:
            raise tool_error(exc) from exc
        except Exception as exc:  # noqa: BLE001 - see failure.unexpected
            raise unexpected("mail_read", exc) from None
