"""Runtime configuration: where the mailbox is, and which folders a client may see."""

import os
from typing import Literal

from pydantic import BaseModel, Field
from second_brain_core.env import env_int, env_list, env_seconds, env_switch, set_but_blank

DEFAULT_IMAP_HOST = "127.0.0.1"
# Proton Mail Bridge's default IMAP port. A hosted IMAP server is usually 993
# with SBI_IMAP_SECURITY=tls, or 143 with the default, starttls.
DEFAULT_IMAP_PORT = 1143
DEFAULT_IMAP_TIMEOUT = 30.0
SECURITY_MODES = ("starttls", "tls")

HIDE_SPAM_AND_TRASH_VAR = "SBI_HIDE_SPAM_AND_TRASH"
FOLDERS_VAR = "SBI_FOLDERS"
EXCLUDE_FOLDERS_VAR = "SBI_EXCLUDE_FOLDERS"


class Settings(BaseModel):
    """Connection and folder-filter settings, all read from ``SBI_*`` variables.

    The username and password may be empty at start-up so that ``serve`` boots
    before the mailbox is configured; a missing credential surfaces as a refused
    sign-in on the first call, not at import time.
    """

    imap_host: str = DEFAULT_IMAP_HOST
    imap_port: int = DEFAULT_IMAP_PORT
    # starttls: a plain connection upgraded before anything else is sent (Bridge's
    # default). tls: TLS from the first byte. There is no unencrypted mode.
    imap_security: Literal["starttls", "tls"] = "starttls"
    imap_username: str = ""
    imap_password: str = Field(default="", repr=False)
    # A PEM file holding the one certificate to trust, instead of the system's
    # certificate authorities. For Bridge, whose certificate is self-signed
    # (`second-brain-imap pin-cert` writes the file).
    imap_tls_cert: str = ""
    imap_timeout: float = DEFAULT_IMAP_TIMEOUT

    # The folder filter (scope.py): mailbox names, each covering the mailbox and
    # everything under it. FOLDERS limits what can be opened; EXCLUDE hides
    # mailboxes and withholds their messages wherever else they appear.
    folders: tuple[str, ...] = ()
    exclude_folders: tuple[str, ...] = ()
    # Filter variables that are set but list no folder. "Only these: none" and
    # "no filter" are opposite readings of that, so it is refused, not guessed.
    blank_filter_vars: tuple[str, ...] = ()
    # Spam and Trash are hidden like SBI_EXCLUDE_FOLDERS entries unless this is
    # plainly switched off.
    hide_spam_and_trash: bool = True
    # Switches set to something that is neither a yes nor a no. They stay on, and
    # the commands refuse to run until the value is fixed.
    unreadable_switches: tuple[str, ...] = ()
    # Other values that can't be read, each as the sentence that says what it should
    # be. The setting keeps its default, and the commands refuse to run until it is
    # fixed. Reading never raises: /healthz reads the settings on every request.
    unreadable_values: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from the ``SBI_*`` environment variables. Never raises."""
        problems: list[str] = []
        raw_security = os.environ.get("SBI_IMAP_SECURITY", "").strip().lower()
        security = raw_security or "starttls"
        if security not in SECURITY_MODES:
            problems.append(
                f"SBI_IMAP_SECURITY must be one of {', '.join(SECURITY_MODES)}, got {security!r}"
            )
            security = "starttls"
        port = env_int("SBI_IMAP_PORT", DEFAULT_IMAP_PORT, minimum=1, maximum=65535)
        if port is None:
            got = os.environ.get("SBI_IMAP_PORT", "")
            problems.append(f"SBI_IMAP_PORT must be a port number from 1 to 65535, got {got!r}")
            port = DEFAULT_IMAP_PORT
        timeout = env_seconds("SBI_IMAP_TIMEOUT", DEFAULT_IMAP_TIMEOUT)
        if timeout is None:
            got = os.environ.get("SBI_IMAP_TIMEOUT", "")
            problems.append(f"SBI_IMAP_TIMEOUT must be a number of seconds above 0, got {got!r}")
            timeout = DEFAULT_IMAP_TIMEOUT
        hide = env_switch(HIDE_SPAM_AND_TRASH_VAR)
        return cls(
            imap_host=os.environ.get("SBI_IMAP_HOST", "").strip() or DEFAULT_IMAP_HOST,
            imap_port=port,
            imap_security=security,  # type: ignore[arg-type]
            imap_username=os.environ.get("SBI_IMAP_USERNAME", "").strip(),
            imap_password=os.environ.get("SBI_IMAP_PASSWORD", ""),
            imap_tls_cert=os.environ.get("SBI_IMAP_TLS_CERT", "").strip(),
            imap_timeout=timeout,
            folders=env_list(FOLDERS_VAR),
            exclude_folders=env_list(EXCLUDE_FOLDERS_VAR),
            blank_filter_vars=set_but_blank(FOLDERS_VAR, EXCLUDE_FOLDERS_VAR),
            hide_spam_and_trash=hide is not False,
            unreadable_switches=(HIDE_SPAM_AND_TRASH_VAR,) if hide is None else (),
            unreadable_values=tuple(problems),
        )
