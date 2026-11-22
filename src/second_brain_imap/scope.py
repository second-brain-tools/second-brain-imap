"""Which mailboxes a client may open, and whose messages are withheld everywhere.

Two settings with two different jobs:

- ``SBI_FOLDERS`` limits what can be **opened**: only the listed mailboxes, each
  with everything under it. Unset, every mailbox can be opened.
- ``SBI_EXCLUDE_FOLDERS`` **hides**: the listed mailboxes can't be opened, and a
  message that is in one of them is withheld wherever else it appears. That
  second half matters because mail servers show one message in several
  mailboxes: with Proton Mail Bridge a message in Trash is also in All Mail and
  in each of its labels. Hiding only the mailbox would leave it readable there.
  Spam and Trash are hidden this way unless ``SBI_HIDE_SPAM_AND_TRASH`` is off.

The filter is resolved against the server's own mailbox list on every call, and
it fails closed: a name that matches no mailbox, a variable set to nothing, or
two settings that contradict each other refuse the call (FolderFilterError)
instead of showing more than was meant.
"""

from dataclasses import dataclass

from .config import EXCLUDE_FOLDERS_VAR, FOLDERS_VAR, HIDE_SPAM_AND_TRASH_VAR, Settings
from .errors import FolderFilterError

# What a mailbox is for, from the special-use flags of RFC 6154.
_ROLES = {
    "\\all": "all",
    "\\archive": "archive",
    "\\drafts": "drafts",
    "\\flagged": "starred",
    "\\junk": "spam",
    "\\sent": "sent",
    "\\trash": "trash",
}
_NOSELECT = frozenset({"\\noselect", "\\nonexistent"})
# What Spam and Trash are called on servers that don't flag them (RFC 6154 is
# optional). Compared in lowercase, against the last part of the mailbox's name.
_SPAM_NAMES = frozenset({"spam", "junk", "junk e-mail", "junk email", "junk mail", "bulk mail"})
_TRASH_NAMES = frozenset({"trash", "deleted", "deleted items", "deleted messages", "bin"})
# Mailboxes that are another view of mail kept elsewhere. A search of everything
# uses the first and skips both when it has to go mailbox by mailbox.
VIEW_ROLES = frozenset({"all", "starred"})


@dataclass(frozen=True)
class FolderInfo:
    """One mailbox as the server lists it."""

    name: str
    flags: frozenset[str]
    delimiter: str

    @property
    def selectable(self) -> bool:
        return not (self.flags & _NOSELECT)

    @property
    def role(self) -> str | None:
        """inbox, sent, drafts, archive, spam, trash, starred or all; None for the rest."""
        if self.name.upper() == "INBOX":
            return "inbox"
        for flag, role in _ROLES.items():
            if flag in self.flags:
                return role
        return None


def folder_info(flags: object, delimiter: object, name: object) -> FolderInfo:
    """A :class:`FolderInfo` from one entry of IMAPClient's ``list_folders``."""

    def text(value: object) -> str:
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)

    listed = flags if isinstance(flags, (tuple, list)) else ()
    return FolderInfo(
        name=text(name),
        flags=frozenset(text(flag).lower() for flag in listed),
        delimiter=text(delimiter) if delimiter else "",
    )


@dataclass(frozen=True)
class FolderScope:
    """The filter, resolved against the mailboxes that exist right now."""

    # Mailboxes a client may open, by exact name.
    visible: frozenset[str]
    # Hidden mailboxes that can hold messages: their messages are withheld everywhere.
    hidden: frozenset[str]

    def allows(self, name: str) -> bool:
        return name in self.visible


def _match(entry: str, folders: list[FolderInfo]) -> FolderInfo | None:
    """The mailbox ``entry`` names: exactly, except that INBOX is case-insensitive."""
    for folder in folders:
        if folder.name == entry or (entry.upper() == "INBOX" and folder.name.upper() == "INBOX"):
            return folder
    return None


def _covered(root: FolderInfo, folders: list[FolderInfo]) -> set[str]:
    """``root`` and every mailbox under it."""
    names = {root.name}
    if root.delimiter:
        prefix = root.name + root.delimiter
        names.update(folder.name for folder in folders if folder.name.startswith(prefix))
    return names


def _resolve(variable: str, entries: tuple[str, ...], folders: list[FolderInfo]) -> set[str]:
    names: set[str] = set()
    for entry in entries:
        folder = _match(entry, folders)
        if folder is None:
            raise FolderFilterError(f"{variable} lists {entry!r}, which is not a mailbox")
        names |= _covered(folder, folders)
    return names


def _with_role(role: str, names: frozenset[str], folders: list[FolderInfo]) -> list[FolderInfo]:
    """The mailboxes the server flags with ``role``; failing that, those named like it."""
    flagged = [folder for folder in folders if folder.role == role]
    if flagged:
        return flagged
    return [
        folder
        for folder in folders
        if folder.role is None
        and (
            folder.name.rsplit(folder.delimiter, 1)[-1] if folder.delimiter else folder.name
        ).lower()
        in names
    ]


def scope_of(settings: Settings, folders: list[FolderInfo]) -> FolderScope:
    """Resolve the folder settings against ``folders``, or refuse.

    Raises :class:`FolderFilterError` when a setting doesn't resolve.
    """
    if settings.blank_filter_vars:
        raise FolderFilterError(
            f"{', '.join(settings.blank_filter_vars)} is set but lists no mailbox; "
            "list at least one, or remove the variable"
        )
    selectable = {folder.name for folder in folders if folder.selectable}
    excluded = _resolve(EXCLUDE_FOLDERS_VAR, settings.exclude_folders, folders)
    for entry in settings.exclude_folders:
        named = _match(entry, folders)
        if named is not None and named.role == "all":
            # Every message is in it, so hiding it would withhold every message.
            raise FolderFilterError(
                f"{EXCLUDE_FOLDERS_VAR} lists {entry!r}, the view of all mail: excluding it "
                f"would hide every message. To keep it closed, leave it out of {FOLDERS_VAR}"
            )
    defaults: set[str] = set()
    if settings.hide_spam_and_trash:
        for role, names in (("spam", _SPAM_NAMES), ("trash", _TRASH_NAMES)):
            found = _with_role(role, names, folders)
            if not found and not (excluded & selectable):
                # "Hidden by default" must not quietly mean "not hidden": a server
                # that doesn't say which mailbox this is has to be told by name.
                raise FolderFilterError(
                    f"{HIDE_SPAM_AND_TRASH_VAR} is on, but the mail server doesn't say which "
                    f"mailbox is {role.capitalize()} and none has a usual name for it. Name the "
                    f"mailboxes to hide in {EXCLUDE_FOLDERS_VAR}, or set "
                    f"{HIDE_SPAM_AND_TRASH_VAR}=false"
                )
            for folder in found:
                defaults |= _covered(folder, folders)
    if settings.folders:
        listed = _resolve(FOLDERS_VAR, settings.folders, folders)
        for entry in settings.folders:
            named = _match(entry, folders)
            assert named is not None  # _resolve refused it otherwise
            if named.name in excluded:
                raise FolderFilterError(
                    f"{FOLDERS_VAR} and {EXCLUDE_FOLDERS_VAR} both list {entry!r}; drop one"
                )
            if named.name in defaults:
                raise FolderFilterError(
                    f"{FOLDERS_VAR} lists {entry!r}, which {HIDE_SPAM_AND_TRASH_VAR} hides; "
                    f"set {HIDE_SPAM_AND_TRASH_VAR}=false to show Spam and Trash"
                )
        allowed = listed & selectable
    else:
        allowed = selectable
    hidden = (excluded | defaults) & selectable
    visible = allowed - hidden
    if not visible:
        raise FolderFilterError("the folder settings leave no mailbox visible")
    return FolderScope(visible=frozenset(visible), hidden=frozenset(hidden))
