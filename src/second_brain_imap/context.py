"""Application context handed to MCP tools via the server lifespan."""

from dataclasses import dataclass
from typing import cast

from fastmcp import Context

from .config import Settings
from .mailbox import Mailbox


@dataclass
class AppContext:
    """Per-server shared state reachable from tools via ``ctx.lifespan_context``."""

    mailbox: Mailbox
    settings: Settings
    # True in HTTP mode, where the caller is a remote client behind sign-in.
    remote: bool = False


def app_context(ctx: Context) -> AppContext:
    """Fetch the :class:`AppContext` from a FastMCP tool context."""
    return cast(AppContext, ctx.lifespan_context)
