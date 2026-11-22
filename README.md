# second-brain-imap

An [MCP](https://modelcontextprotocol.io) server that lets Claude **search and read** an IMAP
mailbox. It cannot send, delete, move or mark anything: there is no code in it that changes a
mailbox.

It was built for [Proton Mail Bridge](https://proton.me/mail/bridge), which decrypts a Proton
mailbox on the machine it runs on and serves it as IMAP on `127.0.0.1`. Any IMAP server with TLS
should work; Bridge is the one it is tested against.

A sibling of [second-brain-joplin](https://github.com/second-brain-tools/second-brain-joplin), on
the same stack: FastMCP, stdio for a client on the same machine, streamable HTTP with GitHub
sign-in for a Claude custom connector.

**Status:** 0.1.0, on [PyPI](https://pypi.org/project/second-brain-imap/). It runs against one
mailbox.

## What Claude gets

| Tool | What it does |
|---|---|
| `mail_list_folders` | The mailboxes (folders and labels) with message and unread counts |
| `mail_search` | Search by text, sender, recipient, subject, dates, folder, unread; newest first |
| `mail_recent` | The last days of one mailbox, newest first |
| `mail_read` | One message as plain text, a page at a time; attachments listed, never downloaded |

Details: [docs/tools.md](docs/tools.md).

## What it is careful about

- **Read-only on the wire.** Mailboxes are opened with `EXAMINE`, data is fetched with
  `BODY.PEEK`, and one small module ([imap.py](src/second_brain_imap/imap.py)) is the only place a
  command leaves from. Reading a message does not mark it as read.
- **Mail is written by strangers.** HTML is reduced to the text a person would see: hidden
  elements, scripts, styles and comments are dropped, and nothing is fetched. Invisible and
  direction-changing characters are removed from every name, subject and body. Every answer says
  that its text is information, not instructions.
- **Spam and Trash are hidden** unless you switch that off, and so is any mailbox you list. A
  hidden message is withheld wherever else the mail server shows a copy of it (Bridge's All Mail
  contains Trash; a label can too).
- **Always encrypted, always verified.** TLS to the mail server with the certificate checked, even
  on `127.0.0.1`. Bridge's self-signed certificate is pinned, not waved through.
- **Bounded.** Attachments are never fetched, a message's text is paged, searches return at most
  50 results, and a remote caller is rate-limited.
- **Quiet logs.** One line per tool call: tool, caller, client, outcome, duration. Never a search
  term, a subject, an address or a credential.

More in [SECURITY.md](SECURITY.md).

## Try it with Proton Mail Bridge

Bridge running and signed in on this machine. Installed from PyPI, a copy reads its settings
from the environment or from the file named with `--env-file`
([docs/configuration.md](docs/configuration.md)):

```bash
uv tool install second-brain-imap
mkdir -p ~/.config/second-brain-imap
# serve.env: SBI_IMAP_USERNAME, SBI_IMAP_PASSWORD (Bridge's, not Proton's) and SBI_IMAP_TLS_CERT,
# a full path where pin-cert saves the certificate (`~` is not expanded in the file).
# .env.example lists every setting.
"${EDITOR:-vi}" ~/.config/second-brain-imap/serve.env
second-brain-imap pin-cert --env-file ~/.config/second-brain-imap/serve.env
second-brain-imap check --env-file ~/.config/second-brain-imap/serve.env
claude mcp add second-brain-imap -- second-brain-imap serve --env-file ~/.config/second-brain-imap/serve.env
```

Or from a checkout, where the `.env` beside `pyproject.toml` is read:

```bash
git clone https://github.com/second-brain-tools/second-brain-imap && cd second-brain-imap
cp .env.example .env        # then set the username and the Bridge password in it
uv run second-brain-imap pin-cert   # saves Bridge's certificate where .env says
uv run second-brain-imap check      # signs in once and prints counts
uv run second-brain-imap folders    # every mailbox, and which a client can open
```

Then add it to Claude Code as a stdio server:

```bash
claude mcp add second-brain-imap -- uv --directory "$PWD" run second-brain-imap serve
```

- Settings: [docs/configuration.md](docs/configuration.md)
- Bridge specifics (the Bridge password, the certificate, how it lays out mail, what its search
  misses): [docs/proton-mail-bridge.md](docs/proton-mail-bridge.md)
- As a Claude custom connector, for claude.ai and the apps:
  [docs/remote-access.md](docs/remote-access.md)

## Development

```bash
uv sync
uv run pytest --cov-fail-under=90   # the suite needs no mail server
uv run mypy
uvx pre-commit run --all-files      # ruff, actionlint and file checks, as CI runs them
uv run scripts/smoke.py             # installs the checkout with uvx and talks MCP to it
```

The tests run against an in-memory mail server ([tests/fakes.py](tests/fakes.py)) that answers in
the shapes a live Bridge 3.27 gave through IMAPClient 4.3.

Sign-in, the audit log, HTTP serving and the settings-file rules are
[second-brain-core](https://github.com/second-brain-tools/second-brain-core)'s, shared with
second-brain-joplin: fix shared code there, release it, then move this package's pin. To try an
unreleased core change, point uv at a checkout for the moment, and don't commit it
(`[tool.uv.sources] second-brain-core = { path = "../second-brain-core", editable = true }`).

CI runs these on GitHub ([checks.yml](.github/workflows/checks.yml)): Python 3.11 to 3.15 on
Linux, and 3.13 on macOS and Windows, then the smoke test of the built wheel on all three.
Dependabot proposes dependency updates weekly. Releases go to PyPI from a signed tag
([docs/releasing.md](docs/releasing.md)).

## License

Copyright (C) 2026 John McKenzie

second-brain-imap is free software: you can redistribute it and/or modify it under the terms of the
GNU Affero General Public License as published by the Free Software Foundation, either version 3 of
the License, or (at your option) any later version. It is distributed in the hope that it will be
useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE) for the full text.

In short: run it and change it as you like. If you pass copies on, or change it and let other
people use your version over a network, you must offer them the source under the same license.
