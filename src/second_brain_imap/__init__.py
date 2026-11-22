"""second-brain-imap: a read-only MCP server for an IMAP mailbox."""

# First, and the only import here: it stops FastMCP, the library underneath, from
# reading its own settings (the transport among them) from a `.env` in the working
# directory, which for an MCP server is whatever project the client has open. It
# must run before anything imports fastmcp, so nothing else is imported in this
# file: import sorting would move such an import above this line.
import second_brain_core  # noqa: F401

__version__ = "0.1.0"
