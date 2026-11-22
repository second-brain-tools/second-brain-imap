"""MCP tool annotations shared by the tool modules.

Clients use these hints, e.g. to skip a confirmation prompt for a read-only tool.
"""

from mcp.types import ToolAnnotations

# Reads the mailbox and changes nothing in it: no flag is set, nothing is moved.
# Not "open world" in the MCP sense: it reaches this one mailbox and nothing else.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
