"""How second-brain-imap describes itself to the shared library (second-brain-core)."""

from second_brain_core.server import Server

from .errors import FolderFilterError, MailError

SERVER = Server(
    name="second-brain-imap",
    prefix="SBI_",
    package="second_brain_imap",
    default_port=8776,  # one above second-brain-joplin's default
)

# This server's own failures: a tool turns them into a ToolError, and the audit
# line names them instead, so an operator can count outages from it.
NAMED_CAUSES = (MailError, FolderFilterError)
