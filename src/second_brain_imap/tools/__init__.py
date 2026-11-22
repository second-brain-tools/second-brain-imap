"""MCP tool registration.

Every tool reads. There is no write tool to register: this server has no code
that changes a mailbox (imap.py).
"""

from fastmcp import FastMCP

from . import read


def register_tools(mcp: FastMCP) -> None:
    """Register the MCP tools on the given FastMCP server."""
    read.register(mcp)
