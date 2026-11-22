"""The folder filter: what can be opened, what is hidden, and when it refuses."""

import pytest
from conftest import settings_for
from fakes import FakeServer

from second_brain_imap.errors import FOLDER_FILTER_MESSAGE, FolderFilterError
from second_brain_imap.scope import FolderInfo, folder_info, scope_of


def listed(server: FakeServer) -> list[FolderInfo]:
    return [folder_info(f.flags, b"/", f.name) for f in server.folders.values()]


def test_roles_and_selectable() -> None:
    assert folder_info((b"\\Junk",), b"/", "Spam").role == "spam"
    assert folder_info((b"\\Flagged",), b"/", "Starred").role == "starred"
    assert folder_info((), b"/", "inbox").role == "inbox"
    assert folder_info((b"\\HasNoChildren",), b".", b"Work").role is None
    assert folder_info((b"\\Noselect",), b"/", "Folders").selectable is False
    assert folder_info((b"\\NonExistent",), b"/", "Ghost").selectable is False
    assert folder_info(None, None, "Odd").delimiter == ""


def test_by_default_everything_but_spam_and_trash(bridge: FakeServer) -> None:
    scope = scope_of(settings_for(), listed(bridge))
    assert scope.hidden == {"Spam", "Trash"}
    assert scope.visible == {
        "INBOX", "Sent", "Drafts", "Archive", "Starred", "All Mail",
        "Folders/Receipts", "Labels/Todo",
    }  # fmt: skip
    assert scope.allows("INBOX") and not scope.allows("Trash") and not scope.allows("Folders")


def test_the_switch_shows_spam_and_trash(bridge: FakeServer) -> None:
    scope = scope_of(settings_for(hide_spam_and_trash=False), listed(bridge))
    assert scope.hidden == set() and {"Spam", "Trash"} <= scope.visible


def test_exclude_covers_what_is_under_it(bridge: FakeServer) -> None:
    scope = scope_of(settings_for(exclude_folders=("Folders", "Archive")), listed(bridge))
    assert scope.hidden == {"Spam", "Trash", "Archive", "Folders/Receipts"}
    assert "Folders/Receipts" not in scope.visible and "Labels/Todo" in scope.visible


def test_folders_limits_what_can_be_opened_but_hides_nothing_more(bridge: FakeServer) -> None:
    scope = scope_of(settings_for(folders=("inbox", "Labels")), listed(bridge))
    assert scope.visible == {"INBOX", "Labels/Todo"}
    # Closed is not hidden: only Spam and Trash have their messages withheld elsewhere.
    assert scope.hidden == {"Spam", "Trash"}


def test_a_prefix_is_not_a_parent(bridge: FakeServer) -> None:
    bridge.folder("Archive2026")
    scope = scope_of(settings_for(exclude_folders=("Archive",)), listed(bridge))
    assert "Archive2026" in scope.visible


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        (
            {"exclude_folders": ("Nope",)},
            "SBI_EXCLUDE_FOLDERS lists 'Nope', which is not a mailbox",
        ),
        ({"folders": ("labels/todo",)}, "SBI_FOLDERS lists 'labels/todo', which is not a mailbox"),
        ({"blank_filter_vars": ("SBI_FOLDERS",)}, "SBI_FOLDERS is set but lists no mailbox"),
        (
            {"folders": ("Archive",), "exclude_folders": ("Archive",)},
            "SBI_FOLDERS and SBI_EXCLUDE_FOLDERS both list 'Archive'",
        ),
        ({"folders": ("Trash",)}, "set SBI_HIDE_SPAM_AND_TRASH=false to show Spam and Trash"),
        ({"folders": ("Folders",), "exclude_folders": ("Folders/Receipts",)}, "no mailbox visible"),
    ],
)
def test_settings_that_do_not_resolve_are_refused(
    bridge: FakeServer, changes: dict[str, object], detail: str
) -> None:
    with pytest.raises(FolderFilterError) as caught:
        scope_of(settings_for(**changes), listed(bridge))
    assert detail in caught.value.detail
    # What a remote client is told names no folder.
    assert str(caught.value) == FOLDER_FILTER_MESSAGE


def test_trash_can_be_listed_once_the_switch_is_off(bridge: FakeServer) -> None:
    scope = scope_of(settings_for(folders=("Trash",), hide_spam_and_trash=False), listed(bridge))
    assert scope.visible == {"Trash"} and scope.hidden == set()


def test_mailboxes_under_spam_and_trash_are_hidden_with_them(bridge: FakeServer) -> None:
    bridge.folder("Trash/Old")
    bridge.folder("Spam/Quarantine")
    scope = scope_of(settings_for(), listed(bridge))
    assert {"Trash/Old", "Spam/Quarantine"} <= scope.hidden
    assert not scope.allows("Trash/Old")


def unflagged(*names: str, delimiter: bytes = b"/") -> list[FolderInfo]:
    """A server that lists its mailboxes without saying what any of them is for."""
    return [folder_info((), delimiter, name) for name in names]


@pytest.mark.parametrize(
    ("names", "delimiter", "hidden"),
    [
        (("INBOX", "Spam", "Trash", "Sent"), b"/", {"Spam", "Trash"}),
        (("INBOX", "Junk", "Deleted Items"), b"/", {"Junk", "Deleted Items"}),
        (("INBOX", "INBOX.Junk E-mail", "INBOX.Trash"), b".", {"INBOX.Junk E-mail", "INBOX.Trash"}),
        (("INBOX", "[Gmail]/Spam", "[Gmail]/Bin"), b"/", {"[Gmail]/Spam", "[Gmail]/Bin"}),
    ],
)
def test_without_flags_spam_and_trash_are_found_by_their_usual_names(
    names: tuple[str, ...], delimiter: bytes, hidden: set[str]
) -> None:
    scope = scope_of(settings_for(), unflagged(*names, delimiter=delimiter))
    assert scope.hidden == hidden


def test_hidden_by_default_never_quietly_means_not_hidden() -> None:
    """A server that neither flags nor names them the usual way has to be told which they are."""
    folders = unflagged("INBOX", "Courrier indésirable", "Corbeille")
    with pytest.raises(FolderFilterError) as caught:
        scope_of(settings_for(), folders)
    assert "SBI_HIDE_SPAM_AND_TRASH is on" in caught.value.detail
    assert "SBI_EXCLUDE_FOLDERS" in caught.value.detail
    # One of the two is enough to refuse: "Trash" is found, Spam is not.
    with pytest.raises(FolderFilterError) as caught:
        scope_of(settings_for(), unflagged("INBOX", "Trash", "Pourriel"))
    assert "which mailbox is Spam" in caught.value.detail
    # The two ways out: name them, or say they need not be hidden.
    named = scope_of(settings_for(exclude_folders=("Courrier indésirable", "Corbeille")), folders)
    assert named.hidden == {"Courrier indésirable", "Corbeille"}
    assert scope_of(settings_for(hide_spam_and_trash=False), folders).hidden == set()


def test_a_flag_wins_over_a_name(bridge: FakeServer) -> None:
    bridge.folder("Folders/Trash")  # a folder of the owner's that happens to be called Trash
    scope = scope_of(settings_for(), listed(bridge))
    assert "Folders/Trash" in scope.visible and scope.hidden == {"Spam", "Trash"}


def test_the_view_of_all_mail_cannot_be_excluded(bridge: FakeServer) -> None:
    """Every message is in it: hiding it would withhold every message."""
    with pytest.raises(FolderFilterError) as caught:
        scope_of(settings_for(exclude_folders=("All Mail",)), listed(bridge))
    assert "would hide every message" in caught.value.detail
    closed = scope_of(settings_for(folders=("INBOX", "Archive")), listed(bridge))
    assert "All Mail" not in closed.visible  # leaving it out of SBI_FOLDERS is how to close it
