"""Typed errors raised by the mail layer and the start-up checks.

A :class:`MailError` carries a sentence the MCP tool layer hands straight to the
client, so it must be safe to show a remote caller: no address, no mailbox name
the caller didn't supply, no credential, no configuration detail.
"""

from second_brain_core.errors import FilterError, ServeConfigError

__all__ = [
    "FolderFilterError",
    "MailError",
    "MailNotFoundError",
    "MailServerError",
    "ServeConfigError",
]


class MailError(Exception):
    """Base class for failures reading the mailbox."""


CONNECTION_MESSAGE = (
    "The mail server isn't answering. On the server's machine, check that the IMAP "
    "server (Proton Mail Bridge, for example) is running: `second-brain-imap check` "
    "says what is wrong."
)


class MailConnectionError(MailError):
    """The IMAP server could not be reached, or the TLS session could not be made."""

    def __init__(self, message: str = CONNECTION_MESSAGE) -> None:
        super().__init__(message)


AUTH_MESSAGE = (
    "The mail server refused the sign-in. On the server's machine, check "
    "SBI_IMAP_USERNAME and SBI_IMAP_PASSWORD (for Proton Mail Bridge, the password "
    "Bridge shows in its window, not the Proton password): `second-brain-imap check` "
    "says what is wrong."
)


class MailAuthError(MailError):
    """The IMAP server rejected the username or password."""

    def __init__(self, message: str = AUTH_MESSAGE) -> None:
        super().__init__(message)


NOT_FOUND_MESSAGE = "No such message or mailbox."
STALE_ID_MESSAGE = (
    "That message id is out of date: the mail server has rebuilt the mailbox since "
    "it was issued. Search again and use the new id."
)


class MailNotFoundError(MailError):
    """The message or mailbox does not exist, or the folder settings hide it.

    Both cases carry the same sentence, so an answer never tells a caller that
    something hidden exists.
    """

    def __init__(self, message: str = NOT_FOUND_MESSAGE) -> None:
        super().__init__(message)


class MailServerError(MailError):
    """The IMAP server answered something this code didn't expect."""


FOLDER_FILTER_MESSAGE = (
    "The server's folder settings (SBI_FOLDERS, SBI_EXCLUDE_FOLDERS, "
    "SBI_HIDE_SPAM_AND_TRASH) don't match the mailbox, so this is refused until they "
    "are fixed. On the server's machine, `second-brain-imap folders` shows what is wrong."
)


class FolderFilterError(FilterError):
    """A folder setting names something the mailbox doesn't have, or contradicts another.

    The tools refuse while this holds: a filter that can't be resolved must not
    fall back to showing everything. ``str()`` is the same fixed sentence every
    time and is safe to hand to a remote client. ``detail`` says which variable
    and entry is wrong; an entry can be the name of a folder the operator wants
    hidden, so the detail is for the operator only (the log, the command line).

    Deliberately not a :class:`MailError`: it is a configuration problem, not a
    mail-server failure.
    """

    MESSAGE = FOLDER_FILTER_MESSAGE
    WHAT = "folder filter"
