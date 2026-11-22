"""Command-line entry point for the IMAP MCP server."""

import argparse
import hashlib
import logging
import os
import ssl
from pathlib import Path

from second_brain_core.audit import configure_logging
from second_brain_core.auth import AuthSettings, build_auth
from second_brain_core.env import load_configuration
from second_brain_core.remote import (
    TRANSPORTS,
    choose_transport,
    http_options,
    is_loopback,
    refuse_http_flags,
    run_http,
    run_stdio,
    validate,
)

from . import __version__, imap
from .config import HIDE_SPAM_AND_TRASH_VAR, Settings
from .errors import FolderFilterError, MailError, ServeConfigError
from .identity import SERVER
from .imap import FolderStatus
from .mailbox import Mailbox
from .scope import FolderInfo, FolderScope, scope_of
from .server import create_server

logger = logging.getLogger("second_brain_imap.cli")

ENV_FILE_HELP = "load settings from this file first (variables already set win)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="second-brain-imap",
        description="A read-only MCP server for an IMAP mailbox.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    serve = subparsers.add_parser(
        "serve",
        help="Start the MCP server (stdio by default, or streamable HTTP).",
        description=(
            "Start the MCP server. stdio is for a client on this machine (Claude Code); "
            "http is for a remote client such as a Claude custom connector "
            "(docs/remote-access.md). Every option falls back to the SBI_* variable named."
        ),
    )
    serve.add_argument(
        "--transport", choices=TRANSPORTS, help="stdio (default) or http  [SBI_TRANSPORT]"
    )
    serve.add_argument("--host", help="HTTP: address to bind (default 127.0.0.1)  [SBI_HOST]")
    serve.add_argument(
        "--port", type=int, help="HTTP: port to listen on (default 8776)  [SBI_PORT]"
    )
    serve.add_argument("--path", help="HTTP: MCP endpoint path (default /mcp)  [SBI_HTTP_PATH]")
    serve.add_argument(
        "--public-url",
        help="HTTP: the server's public origin, e.g. https://mail.example.com  [SBI_PUBLIC_URL]",
    )
    serve.add_argument(
        "--no-auth",
        action="store_true",
        help="HTTP: run without sign-in, on a loopback address only (local testing)",
    )
    serve.add_argument("--env-file", type=Path, help=ENV_FILE_HELP)
    check = subparsers.add_parser(
        "check",
        help="Sign in to the mail server once and say whether it worked.",
        description=(
            "Connect to the IMAP server, sign in and resolve the folder settings. Prints "
            "counts only, and on a failure the reason. Exit status 0 when all of it worked."
        ),
    )
    check.add_argument("--env-file", type=Path, help=ENV_FILE_HELP)
    folders = subparsers.add_parser(
        "folders",
        help="List the mailboxes, and which of them a client can open.",
        description=(
            "Print every mailbox with its message count, marked visible, closed (not in "
            "SBI_FOLDERS) or hidden (SBI_EXCLUDE_FOLDERS, or Spam and Trash): the names "
            "to use in those variables, and a check of them before a client sees through "
            "them. The first lines are counts only."
        ),
    )
    folders.add_argument("--env-file", type=Path, help=ENV_FILE_HELP)
    pin = subparsers.add_parser(
        "pin-cert",
        help="Save the certificate of a mail server on this machine, to trust it by name.",
        description=(
            "Fetch the certificate the IMAP server presents and write it to the file named "
            "by SBI_IMAP_TLS_CERT, which is then the only certificate the server trusts. "
            "For Proton Mail Bridge, whose certificate is self-signed. Only for a server "
            "on this machine (a loopback address): across a network, the first answer "
            "could be anybody's."
        ),
    )
    pin.add_argument(
        "--replace",
        action="store_true",
        help="overwrite a saved certificate that differs (after the mail server was reinstalled)",
    )
    pin.add_argument("--env-file", type=Path, help=ENV_FILE_HELP)
    return parser


def refuse_unreadable_settings(settings: Settings) -> None:
    """Refuse a setting whose value can't be read: a switch, a port, a timeout, a mode.

    The setting itself keeps its safe value (config.py): a switch stays on, a number
    keeps its default, so a typo exposes nothing. Refusing is so the operator finds
    out that the value didn't do what they meant.
    """
    problems = [
        f"{name} must be true or false, got {os.environ.get(name, '')!r}"
        for name in settings.unreadable_switches
    ] + list(settings.unreadable_values)
    if problems:
        raise ServeConfigError("; ".join(problems))


def reason(exc: BaseException) -> str:
    """``exc`` for the operator's terminal: its sentence, and what was underneath it."""
    cause = exc.__cause__
    if cause is None:
        return str(exc)
    return f"{exc} ({type(cause).__name__}: {cause})"


def mark_of(folder: FolderInfo, scope: FolderScope | None) -> str:
    """How the filter treats ``folder``; ``?`` when the settings don't resolve."""
    if not folder.selectable:
        return "-"
    if scope is None:
        return "?"
    if folder.name in scope.hidden:
        return "hidden"
    return "visible" if scope.allows(folder.name) else "closed"


def folder_lines(
    inventory: list[tuple[FolderInfo, FolderStatus | None]], scope: FolderScope | None
) -> list[str]:
    """One line per mailbox: mark, message count, name. Hidden ones too."""
    lines = []
    for folder, status in inventory:
        count = "" if status is None else str(status.messages)
        lines.append(f"{mark_of(folder, scope):<8}  {count:>7}  {folder.name}")
    return lines


def summary_lines(
    inventory: list[tuple[FolderInfo, FolderStatus | None]], scope: FolderScope, settings: Settings
) -> list[str]:
    """What a client can reach, as counts only (safe to show in a deployment log)."""
    selectable = [folder for folder, _ in inventory if folder.selectable]
    lines = [f"Mailboxes a client can open: {len(scope.visible)} of {len(selectable)}."]
    if scope.hidden:
        lines.append(
            f"Hidden: {len(scope.hidden)}; their messages are withheld wherever they appear."
        )
    else:
        lines.append("Hidden: none.")
    if not settings.hide_spam_and_trash:
        lines.append(f"Spam and Trash: not hidden ({HIDE_SPAM_AND_TRASH_VAR} is off).")
    return lines


def folders(_args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    refuse_unreadable_settings(settings)
    inventory = Mailbox(settings).inventory()
    try:
        scope = scope_of(settings, [folder for folder, _ in inventory])
    except FolderFilterError:
        # Show the list anyway: its names are what fixes the settings.
        print("\n".join(folder_lines(inventory, None)))
        raise
    print("\n".join(summary_lines(inventory, scope, settings)))
    print("\n".join(folder_lines(inventory, scope)))


def check(_args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    refuse_unreadable_settings(settings)
    inventory = Mailbox(settings).inventory()
    scope = scope_of(settings, [folder for folder, _ in inventory])
    print("Signed in to the mail server.")
    print("\n".join(summary_lines(inventory, scope, settings)))


def fingerprint(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))


def pin_cert(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    refuse_unreadable_settings(settings)
    if not settings.imap_tls_cert:
        raise ServeConfigError(
            "set SBI_IMAP_TLS_CERT to the file the certificate should be kept in"
        )
    if not is_loopback(settings.imap_host):
        raise ServeConfigError(
            f"pin-cert only trusts a mail server on this machine, not {settings.imap_host}: "
            "get that server's certificate from whoever runs it"
        )
    path = Path(settings.imap_tls_cert)
    der = imap.fetch_certificate(settings)
    if path.exists():
        try:
            saved = ssl.PEM_cert_to_DER_cert(path.read_text())
        except (OSError, ValueError) as exc:
            raise ServeConfigError(f"{path} exists but is not a certificate: {exc}") from None
        if saved == der:
            print(f"Already saved: {path}\nSHA-256 {fingerprint(der)}")
            return
        if not args.replace:
            raise ServeConfigError(
                f"the mail server's certificate differs from the one saved in {path}.\n"
                f"  saved:  SHA-256 {fingerprint(saved)}\n"
                f"  server: SHA-256 {fingerprint(der)}\n"
                "If the mail server was reinstalled or reset, run this again with --replace."
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Created private from the start. The certificate isn't secret; the file is
    # what the server trusts, so nobody else should be able to swap it.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(ssl.DER_cert_to_PEM_cert(der))
    print(f"Saved: {path}\nSHA-256 {fingerprint(der)}")


def check_at_startup(settings: Settings) -> None:
    """Refuse to serve with folder settings the mailbox doesn't match.

    Only when the mail server answers: with it down, the server starts anyway
    and every call fails closed until it is back (mailbox.py).
    """
    try:
        inventory = Mailbox(settings).inventory()
    except MailError as exc:
        logger.warning("the mail server isn't answering yet: %s", reason(exc))
        return
    scope = scope_of(settings, [folder for folder, _ in inventory])
    for line in summary_lines(inventory, scope, settings):
        logger.info(line)


def serve(args: argparse.Namespace) -> None:
    transport = choose_transport(SERVER, args.transport)
    settings = Settings.from_env()
    refuse_unreadable_settings(settings)
    if transport == "stdio":
        refuse_http_flags(
            {
                "--host": args.host,
                "--port": args.port,
                "--path": args.path,
                "--public-url": args.public_url,
                "--no-auth": args.no_auth or None,
            }
        )
        check_at_startup(settings)
        # Never FastMCP's own FASTMCP_TRANSPORT: run_stdio names the transport.
        run_stdio(create_server())
        return
    options = http_options(
        SERVER,
        host=args.host,
        port=args.port,
        path=args.path,
        public_url=args.public_url,
        no_auth=args.no_auth,
    )
    auth_settings = AuthSettings.from_env(SERVER)
    validate(SERVER, options, auth_configured=auth_settings is not None)
    configure_logging(SERVER, no_debug=("imapclient",))
    check_at_startup(settings)
    auth = None
    if auth_settings is not None:
        assert options.public_url is not None  # validate() guarantees it with sign-in
        auth = build_auth(SERVER, auth_settings, options.public_url)
    server = create_server(
        auth=auth,
        allowed_ids=auth_settings.allowed_ids if auth_settings else None,
        rate_limit_per_minute=options.rate_limit_per_minute,
        remote=True,
    )
    run_http(server, options)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return
    try:
        # The named file is the configuration. A checkout's .env is not read as
        # well: it would fill in whatever the file leaves out.
        load_configuration(SERVER, getattr(args, "env_file", None))
        if args.command == "serve":
            serve(args)
        elif args.command == "check":
            check(args)
        elif args.command == "folders":
            folders(args)
        elif args.command == "pin-cert":
            pin_cert(args)
    except ServeConfigError as exc:
        parser.exit(2, f"{parser.prog}: error: {exc}\n")
    except FolderFilterError as exc:
        # A configuration error like the ones above. The detail names the entry at
        # fault; this is the operator's terminal, so it may.
        parser.exit(2, f"{parser.prog}: error: {exc.detail}\n")
    except MailError as exc:
        parser.exit(1, f"{parser.prog}: error: {reason(exc)}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
