"""The command line: serve, check, folders, pin-cert, and where settings may come from."""

import logging
import ssl
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from fakes import PASSWORD, USERNAME, FakeServer
from second_brain_core import env as core_env

from second_brain_imap import cli, imap
from second_brain_imap.errors import CONNECTION_MESSAGE, MailConnectionError

SIGN_IN = {
    "SBI_GITHUB_CLIENT_ID": "client-id",
    "SBI_GITHUB_CLIENT_SECRET": "client-secret-SECRET",
    "SBI_JWT_SIGNING_KEY": "k" * 40,
    "SBI_ALLOWED_GITHUB_IDS": "65598",
}
EVERY_VARIABLE = (
    "SBI_TRANSPORT", "SBI_HOST", "SBI_PORT", "SBI_HTTP_PATH", "SBI_PUBLIC_URL",
    "SBI_RATE_LIMIT_PER_MINUTE", "SBI_OAUTH_REDIRECT_URIS", "SBI_IMAP_HOST", "SBI_IMAP_PORT",
    "SBI_IMAP_SECURITY", "SBI_IMAP_TLS_CERT", "SBI_IMAP_TIMEOUT", *SIGN_IN,
)  # fmt: skip


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in EVERY_VARIABLE:
        monkeypatch.delenv(name, raising=False)
    # Tests run from a checkout: its own .env must not leak into them.
    monkeypatch.setattr(core_env, "load_dotenv", lambda *a, **k: None)


def run(*argv: str) -> None:
    cli.main(list(argv))


def exit_code(*argv: str) -> int:
    with pytest.raises(SystemExit) as caught:
        run(*argv)
    assert isinstance(caught.value.code, int)
    return caught.value.code


# ── the basics ───────────────────────────────────────────────────────────────────────────────────


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    run()
    out = capsys.readouterr().out
    for command in ("serve", "check", "folders", "pin-cert"):
        assert command in out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert exit_code("--version") == 0
    assert capsys.readouterr().out.startswith("second-brain-imap 0.")


# ── check and folders ────────────────────────────────────────────────────────────────────────────


def test_check_prints_counts_only(live: FakeServer, capsys: pytest.CaptureFixture[str]) -> None:
    run("check")
    assert capsys.readouterr().out.splitlines() == [
        "Signed in to the mail server.",
        "Mailboxes a client can open: 8 of 10.",
        "Hidden: 2; their messages are withheld wherever they appear.",
    ]


def test_check_says_when_spam_and_trash_are_shown(
    live: FakeServer, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SBI_HIDE_SPAM_AND_TRASH", "false")
    run("check")
    assert capsys.readouterr().out.splitlines()[1:] == [
        "Mailboxes a client can open: 10 of 10.",
        "Hidden: none.",
        "Spam and Trash: not hidden (SBI_HIDE_SPAM_AND_TRASH is off).",
    ]


def test_check_fails_with_the_reason(live: FakeServer, capsys: pytest.CaptureFixture[str]) -> None:
    live.down = True
    assert exit_code("check") == 1
    assert CONNECTION_MESSAGE in capsys.readouterr().err
    live.down, live.refuse_login = False, True
    assert exit_code("check") == 1
    assert "refused the sign-in" in capsys.readouterr().err


def test_the_reason_names_what_was_underneath() -> None:
    try:
        raise MailConnectionError from ConnectionRefusedError(61, "Connection refused")
    except MailConnectionError as exc:
        text = cli.reason(exc)
    assert text.startswith(CONNECTION_MESSAGE) and "ConnectionRefusedError" in text
    assert cli.reason(MailConnectionError()) == CONNECTION_MESSAGE


def test_folders_marks_every_mailbox(
    live: FakeServer, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SBI_FOLDERS", "INBOX,Labels,Trash,Spam,All Mail")
    monkeypatch.setenv("SBI_EXCLUDE_FOLDERS", "Labels/Todo")
    monkeypatch.setenv("SBI_HIDE_SPAM_AND_TRASH", "off")
    run("folders")
    lines = capsys.readouterr().out.splitlines()
    assert lines[:3] == [
        "Mailboxes a client can open: 4 of 10.",
        "Hidden: 1; their messages are withheld wherever they appear.",
        "Spam and Trash: not hidden (SBI_HIDE_SPAM_AND_TRASH is off).",
    ]
    marks = {
        line.split(None, 2)[-1] if line[0] != "-" else line.split()[-1]: line.split()[0]
        for line in lines[3:]
    }
    assert marks["INBOX"] == "visible" and marks["Trash"] == "visible"
    assert marks["Archive"] == "closed"  # not in SBI_FOLDERS
    assert marks["Labels/Todo"] == "hidden"
    assert marks["Folders"] == "-" and marks["Labels"] == "-"
    inbox = next(line for line in lines if line.endswith("INBOX"))
    assert inbox.split() == ["visible", "2", "INBOX"]


def test_folders_with_settings_that_do_not_resolve_still_lists_the_names(
    live: FakeServer, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SBI_EXCLUDE_FOLDERS", "Recepits")
    assert exit_code("folders") == 2
    captured = capsys.readouterr()
    assert "SBI_EXCLUDE_FOLDERS lists 'Recepits', which is not a mailbox" in captured.err
    listed = [line.split(None, 2) for line in captured.out.splitlines()]
    assert ["?", "2", "INBOX"] in listed and ["?", "1", "Folders/Receipts"] in listed
    assert exit_code("check") == 2


@pytest.mark.parametrize("command", ["check", "folders", "serve"])
def test_a_switch_that_is_neither_yes_nor_no_is_refused(
    live: FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    monkeypatch.setenv("SBI_HIDE_SPAM_AND_TRASH", "flase")
    assert exit_code(command) == 2
    assert "SBI_HIDE_SPAM_AND_TRASH must be true or false, got 'flase'" in capsys.readouterr().err
    assert live.connections == 0


# ── pin-cert ─────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def certificate(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    presented = {"der": b"certificate-one"}
    monkeypatch.setattr(imap, "fetch_certificate", lambda settings: presented["der"])
    return presented


def test_pin_cert_saves_then_recognises_then_refuses_a_change(
    certificate: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "config" / "bridge-cert.pem"
    monkeypatch.setenv("SBI_IMAP_TLS_CERT", str(path))
    run("pin-cert")
    out = capsys.readouterr().out
    assert out.startswith(f"Saved: {path}\nSHA-256 ") and out.count(":") >= 31
    assert ssl.PEM_cert_to_DER_cert(path.read_text()) == b"certificate-one"
    if sys.platform != "win32":  # Windows has no POSIX file modes
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    run("pin-cert")
    assert capsys.readouterr().out.startswith("Already saved:")

    certificate["der"] = b"certificate-two"
    assert exit_code("pin-cert") == 2
    err = capsys.readouterr().err
    assert "differs from the one saved" in err and "--replace" in err
    assert err.count("SHA-256") == 2
    assert ssl.PEM_cert_to_DER_cert(path.read_text()) == b"certificate-one"  # untouched

    run("pin-cert", "--replace")
    assert ssl.PEM_cert_to_DER_cert(path.read_text()) == b"certificate-two"


def test_pin_cert_refuses_what_it_should(
    certificate: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert exit_code("pin-cert") == 2
    assert "set SBI_IMAP_TLS_CERT" in capsys.readouterr().err

    path = tmp_path / "cert.pem"
    monkeypatch.setenv("SBI_IMAP_TLS_CERT", str(path))
    monkeypatch.setenv("SBI_IMAP_HOST", "imap.example.com")
    assert exit_code("pin-cert") == 2
    assert "only trusts a mail server on this machine" in capsys.readouterr().err
    assert not path.exists()

    monkeypatch.setenv("SBI_IMAP_HOST", "localhost")
    path.write_text("this is not a certificate")
    assert exit_code("pin-cert") == 2
    assert "is not a certificate" in capsys.readouterr().err


def test_pin_cert_when_the_mail_server_is_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(settings: Any) -> bytes:
        raise MailConnectionError from ConnectionRefusedError("refused")

    monkeypatch.setattr(imap, "fetch_certificate", refuse)
    monkeypatch.setenv("SBI_IMAP_TLS_CERT", str(tmp_path / "cert.pem"))
    assert exit_code("pin-cert") == 1
    assert "ConnectionRefusedError" in capsys.readouterr().err


# ── serve ────────────────────────────────────────────────────────────────────────────────────────


class Stub:
    """Stands in for the FastMCP server ``serve`` builds."""

    def __init__(self, **kwargs: Any) -> None:
        self.created_with = kwargs
        self.ran_with: dict[str, Any] | None = None

    def run(self, **kwargs: Any) -> None:
        self.ran_with = kwargs


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> list[Stub]:
    made: list[Stub] = []

    def create_server(**kwargs: Any) -> Stub:
        made.append(Stub(**kwargs))
        return made[-1]

    monkeypatch.setattr(cli, "create_server", create_server)
    monkeypatch.setattr(
        cli, "run_http", lambda server, options: setattr(server, "ran_with", options)
    )
    return made


def test_serve_is_stdio_unless_told_otherwise(live: FakeServer, created: list[Stub]) -> None:
    run("serve")
    (server,) = created
    assert server.created_with == {} and server.ran_with == {"transport": "stdio"}


def test_serve_stdio_ignores_fastmcps_own_transport_setting(
    live: FakeServer, created: list[Stub], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FASTMCP_TRANSPORT", "http")
    run("serve")
    assert created[0].ran_with == {"transport": "stdio"}


@pytest.mark.parametrize(
    "flags",
    [
        ["--host", "0.0.0.0"],
        ["--port", "9"],
        ["--path", "/x"],
        ["--no-auth"],
        ["--public-url", "https://m.example"],
    ],
)
def test_serve_stdio_refuses_http_options(
    live: FakeServer, created: list[Stub], capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    assert exit_code("serve", *flags) == 2
    assert f"{flags[0]} only apply to --transport http" in capsys.readouterr().err
    assert created == []


def test_serve_refuses_an_unknown_transport(
    live: FakeServer, created: list[Stub], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBI_TRANSPORT", "carrier-pigeon")
    assert exit_code("serve") == 2 and created == []


def test_serve_checks_the_folder_settings_before_it_starts(
    live: FakeServer, created: list[Stub], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBI_FOLDERS", "No Such Folder")
    assert exit_code("serve") == 2
    assert exit_code("serve", "--transport", "http", "--no-auth") == 2
    assert created == []


def test_serve_starts_while_the_mail_server_is_down(
    live: FakeServer, created: list[Stub], caplog: pytest.LogCaptureFixture
) -> None:
    """Bridge may come up after the server (both start at login): every call fails closed."""
    live.down = True
    caplog.set_level(logging.WARNING, logger="second_brain_imap.cli")
    run("serve")
    assert created[0].ran_with == {"transport": "stdio"}
    assert "the mail server isn't answering yet" in caplog.text


def test_serve_http_needs_sign_in_or_an_explicit_no_auth(
    live: FakeServer, created: list[Stub], capsys: pytest.CaptureFixture[str]
) -> None:
    assert exit_code("serve", "--transport", "http") == 2
    assert "HTTP mode needs GitHub sign-in configured" in capsys.readouterr().err
    run("serve", "--transport", "http", "--no-auth", "--port", "9105")
    (server,) = created
    assert server.created_with == {
        "auth": None,
        "allowed_ids": None,
        "rate_limit_per_minute": 60,
        "remote": True,
    }
    assert server.ran_with.port == 9105 and server.ran_with.no_auth is True  # type: ignore[union-attr]


def test_serve_http_with_sign_in(
    live: FakeServer, created: list[Stub], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in SIGN_IN.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SBI_TRANSPORT", "http")
    assert exit_code("serve") == 2  # sign-in needs the public URL
    monkeypatch.setenv("SBI_PUBLIC_URL", "https://mail.example.com")
    monkeypatch.setenv("SBI_RATE_LIMIT_PER_MINUTE", "30")
    monkeypatch.setattr(cli, "build_auth", lambda server, settings, url: ("auth for", url))
    run("serve")
    (server,) = created
    assert server.created_with == {
        "auth": ("auth for", "https://mail.example.com"),
        "allowed_ids": frozenset({"65598"}),
        "rate_limit_per_minute": 30,
        "remote": True,
    }
    assert exit_code("serve", "--no-auth") == 2  # sign-in and --no-auth contradict


# ── where settings come from ─────────────────────────────────────────────────────────────────────


def test_env_file_fills_in_what_is_not_set(
    live: FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("SBI_IMAP_USERNAME")
    monkeypatch.setenv("SBI_IMAP_PASSWORD", PASSWORD)  # set: the file must not replace it
    monkeypatch.setenv("SBI_FOLDERS", "")  # set to nothing: the file's value counts
    env = tmp_path / "serve.env"
    env.write_text(f"SBI_IMAP_USERNAME={USERNAME}\nSBI_IMAP_PASSWORD=wrong\nSBI_FOLDERS=INBOX\n")
    run("check", "--env-file", str(env))
    assert "Mailboxes a client can open: 1 of 10." in capsys.readouterr().out


def test_an_env_file_named_on_the_command_line_is_the_whole_configuration(
    live: FakeServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A checkout's .env is not read as well: it would fill in whatever the file leaves out."""
    read: list[str] = []
    monkeypatch.setattr(core_env, "load_dotenv", lambda *a, **k: read.append(".env"))
    env = tmp_path / "serve.env"
    env.write_text("SBI_FOLDERS=INBOX\n")
    run("check", "--env-file", str(env))
    assert read == []
    run("check")
    assert read == [".env"]


def test_a_missing_env_file_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert exit_code("check", "--env-file", str(tmp_path / "nope.env")) == 2
    assert "does not exist or is not a file" in capsys.readouterr().err


# Where a checkout's .env is looked for, and that an installed copy reads none, are
# second-brain-core's rules (its tests/test_env.py); tests/test_core_checks.py runs
# them on this package.
