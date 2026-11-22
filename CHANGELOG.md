# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **The text cleaner is second-brain-core's** (0.2.0), shared with the calendar server, instead of
  this package's own copy (#9). Behaviour is unchanged, except that HTML that can't be read now
  becomes "[This HTML could not be turned into text.]". The invisible-character test is core's
  shared check, which now also catches line and paragraph separators.

## [0.1.0] — First release

The first working server: read-only access to an IMAP mailbox for an MCP client, built for Proton
Mail Bridge. It is licensed under the GNU Affero General Public License, version 3 or later.

### Added

- **Four read-only tools**: `mail_list_folders`, `mail_search` (text, sender, recipient, subject,
  dates, folder, unread; newest first), `mail_recent` and `mail_read` (plain text in pages,
  attachments listed by name, type and size). See [docs/tools.md](docs/tools.md).
- **A connection that can only read.** Mailboxes are opened with `EXAMINE` and data fetched with
  `BODY.PEEK`; one module is the only place IMAP commands leave from, and tests fail if it gains a
  call that writes. Reading does not mark a message as read. Search terms are sent as IMAP
  literals, so nothing a caller types can be read as IMAP syntax.
- **Mail turned into text a person could have seen.** Of a message's forms, the one a mail client
  shows (the HTML) is read. HTML is read the way a browser builds the page; elements styled
  invisible, scripts, styles and comments are dropped; links and images become text; nothing is
  fetched. Invisible and direction-changing characters are removed from every name, subject and
  body. Every answer says its text is information, not instructions. Headers and HTML are read in
  time proportional to their size, whatever a sender puts in them.
- **A folder filter that fails closed**: `SBI_FOLDERS` (only these can be opened),
  `SBI_EXCLUDE_FOLDERS` (hidden, and their messages withheld wherever else the mail server shows
  them) and `SBI_HIDE_SPAM_AND_TRASH` (on by default). What is hidden is read after the messages
  to be shown are collected, so mail that lands in Spam during a call is never shown by it. See
  [docs/configuration.md](docs/configuration.md#folders).
- **TLS always verified**, with `SBI_IMAP_TLS_CERT` to trust one self-signed certificate and
  `second-brain-imap pin-cert` to save it from a mail server on the same machine.
- **Errors that say nothing they shouldn't.** A hidden mailbox, a missing one and a message that
  doesn't exist get the same answer. A failure nobody planned for gives the client a fixed
  sentence and the log its type and place, never its text.
- **Commands for the operator**: `check` (sign in once, print counts) and `folders` (every
  mailbox and how the settings treat it).
- **HTTP mode for a Claude custom connector**, carried over from second-brain-joplin: stateless
  streamable HTTP on loopback, GitHub sign-in limited to listed user ids, a per-caller rate limit,
  a `/healthz` route, and a log with one line per tool call and no content. See
  [docs/remote-access.md](docs/remote-access.md).

- **Built on [second-brain-core](https://github.com/second-brain-tools/second-brain-core)** 0.1.1
  or later: sign-in, the audit log, the rate limit, HTTP serving, the settings files and the
  FastMCP guards are shared with second-brain-joplin, so a fix lands in both at once.
- **The audit line names the cause of a failure** by class (`outcome=error:MailNotFoundError`,
  `error:FolderFilterError`), **and the client** (`client=`): the name it registered under at
  sign-in, else the one it gives in the MCP handshake, else the first word of its `User-Agent`,
  else `unknown`. Cut to 40 safe characters; what the client says about itself, never proof. It
  names a sign-in, not an app: the Claude apps that share your account's connector all show
  `client=Claude`.
- **A setting that can't be read is refused by the command.** A bad `SBI_IMAP_PORT`,
  `SBI_IMAP_TIMEOUT` or `SBI_IMAP_SECURITY` keeps its default and is recorded; `serve`, `check`,
  `folders` and `pin-cert` refuse to start and say which. `/healthz` reads such settings as down.
- **An unplanned failure gives a fixed sentence**: "second-brain-imap couldn't complete this
  request. The server's log says where." FastMCP's "Error calling tool" log line carries no
  traceback, so no exception's text reaches the log.
- **The check that a working directory's `.env` can't set FastMCP's settings** runs in this
  repository's suite, as a subprocess, through `second_brain_core.testing`.
- **Public on GitHub and released on PyPI**: `uvx second-brain-imap` or `uv tool install
  second-brain-imap`. CI runs on Linux, macOS and Windows; a signed `v*` tag publishes to PyPI
  through a trusted publisher ([docs/releasing.md](docs/releasing.md)). The docs say what
  `/healthz` proves and what it doesn't.

[Unreleased]: https://github.com/second-brain-tools/second-brain-imap/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/second-brain-tools/second-brain-imap/releases/tag/v0.1.0
