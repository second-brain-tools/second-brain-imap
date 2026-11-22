"""Settings from the environment."""

import pytest

from second_brain_imap.config import Settings

VARIABLES = (
    "SBI_IMAP_HOST", "SBI_IMAP_PORT", "SBI_IMAP_SECURITY", "SBI_IMAP_USERNAME",
    "SBI_IMAP_PASSWORD", "SBI_IMAP_TLS_CERT", "SBI_IMAP_TIMEOUT", "SBI_FOLDERS",
    "SBI_EXCLUDE_FOLDERS", "SBI_HIDE_SPAM_AND_TRASH",
)  # fmt: skip


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_bridge_on_this_machine() -> None:
    settings = Settings.from_env()
    assert (settings.imap_host, settings.imap_port, settings.imap_security) == (
        "127.0.0.1",
        1143,
        "starttls",
    )
    assert settings.imap_username == "" and settings.imap_password == ""
    assert settings.folders == () and settings.exclude_folders == ()
    assert settings.hide_spam_and_trash is True and settings.unreadable_switches == ()
    assert settings.imap_timeout == 30.0


def test_everything_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBI_IMAP_HOST", " imap.example.com ")
    monkeypatch.setenv("SBI_IMAP_PORT", "993")
    monkeypatch.setenv("SBI_IMAP_SECURITY", "TLS")
    monkeypatch.setenv("SBI_IMAP_USERNAME", " me@example.com ")
    monkeypatch.setenv("SBI_IMAP_PASSWORD", " kept as typed ")
    monkeypatch.setenv("SBI_IMAP_TLS_CERT", "/tmp/cert.pem")
    monkeypatch.setenv("SBI_IMAP_TIMEOUT", "5.5")
    monkeypatch.setenv("SBI_FOLDERS", "INBOX, Folders/Receipts ,,")
    monkeypatch.setenv("SBI_EXCLUDE_FOLDERS", "Labels/Private")
    monkeypatch.setenv("SBI_HIDE_SPAM_AND_TRASH", "false")
    settings = Settings.from_env()
    assert (settings.imap_host, settings.imap_port, settings.imap_security) == (
        "imap.example.com",
        993,
        "tls",
    )
    assert settings.imap_username == "me@example.com"
    assert settings.imap_password == " kept as typed "
    assert settings.imap_tls_cert == "/tmp/cert.pem" and settings.imap_timeout == 5.5
    assert settings.folders == ("INBOX", "Folders/Receipts")
    assert settings.exclude_folders == ("Labels/Private",)
    assert settings.hide_spam_and_trash is False


def test_the_password_stays_out_of_a_printed_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBI_IMAP_PASSWORD", "hunter2-SECRET")
    assert "hunter2" not in repr(Settings.from_env())
    assert "hunter2" not in str(Settings.from_env())


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("SBI_IMAP_SECURITY", "none", "SBI_IMAP_SECURITY must be one of starttls, tls"),
        ("SBI_IMAP_SECURITY", "plain", "SBI_IMAP_SECURITY must be one of"),
        ("SBI_IMAP_PORT", "imap", "SBI_IMAP_PORT must be a port number from 1 to 65535"),
        ("SBI_IMAP_PORT", "0", "SBI_IMAP_PORT must be a port number from 1 to 65535"),
        ("SBI_IMAP_PORT", "70000", "SBI_IMAP_PORT must be a port number from 1 to 65535"),
        ("SBI_IMAP_TIMEOUT", "soon", "SBI_IMAP_TIMEOUT must be a number of seconds above 0"),
        ("SBI_IMAP_TIMEOUT", "0", "SBI_IMAP_TIMEOUT must be a number of seconds above 0"),
    ],
)
def test_values_that_are_recorded_not_raised(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    """Reading never raises (/healthz reads the settings on every request): the value
    keeps its default and is recorded, and the commands refuse to run (test_cli.py)."""
    monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    (problem,) = settings.unreadable_values
    assert problem.startswith(message) and repr(value) in problem
    assert (settings.imap_security, settings.imap_port, settings.imap_timeout) == (
        "starttls",
        1143,
        30.0,
    )


def test_a_filter_variable_set_to_nothing_is_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBI_FOLDERS", " , ")
    monkeypatch.setenv("SBI_EXCLUDE_FOLDERS", "")
    assert Settings.from_env().blank_filter_vars == ("SBI_FOLDERS", "SBI_EXCLUDE_FOLDERS")


@pytest.mark.parametrize(
    ("value", "hides", "unreadable"),
    [
        ("", True, False),
        ("true", True, False),
        ("ON", True, False),
        ("1", True, False),
        ("false", False, False),
        ("No", False, False),
        ("0", False, False),
        ("off", False, False),
        ("flase", True, True),  # a typo keeps the protection on, and is reported
        ("maybe", True, True),
    ],
)
def test_the_spam_and_trash_switch(
    monkeypatch: pytest.MonkeyPatch, value: str, hides: bool, unreadable: bool
) -> None:
    monkeypatch.setenv("SBI_HIDE_SPAM_AND_TRASH", value)
    settings = Settings.from_env()
    assert settings.hide_spam_and_trash is hides
    assert bool(settings.unreadable_switches) is unreadable
