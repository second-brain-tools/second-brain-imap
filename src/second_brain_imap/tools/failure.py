"""Turning a failure inside a tool into the error an MCP client sees.

second-brain-core's rules (``second_brain_core.failure``), bound to this server: a
planned failure carries its own sentence, a folder filter that doesn't resolve
tells the client nothing more, and anything unplanned gets a fixed sentence (an
exception's own text can hold a header, a file name or a search term).
"""

from fastmcp.exceptions import ToolError
from second_brain_core import failure

from ..identity import SERVER

INTERNAL_MESSAGE = failure.internal_message(SERVER)


def tool_error(exc: Exception) -> ToolError:
    """The ``ToolError`` for a planned failure (see ``second_brain_core.failure``)."""
    return failure.tool_error(SERVER, exc)


def unexpected(tool: str, exc: Exception) -> ToolError:
    """The ``ToolError`` for a failure nobody planned for: a fixed sentence."""
    return failure.unexpected(SERVER, tool, exc)
