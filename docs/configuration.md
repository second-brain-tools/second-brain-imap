# Configuration

Everything is set through `SBI_*` environment variables. Where they come from:

- **An installed copy** (`uv tool install`, `uvx`): its environment, or the file named by
  `--env-file` on any command. It reads no `.env` on its own, from anywhere.
- **A source checkout**: also the `.env` beside its `pyproject.toml` (copy `.env.example`), unless
  `--env-file` is given. A file named on the command line is the whole configuration.

A variable already set to a value wins over the same variable in a file. A variable set to nothing
doesn't count as set, so an empty export can't silently replace what a file says.

`second-brain-imap check` signs in once with the settings as they are and prints counts;
`second-brain-imap folders` lists every mailbox and how the settings treat it. Run them after any
change.

## The mailbox

| Variable | Default | |
|---|---|---|
| `SBI_IMAP_HOST` | `127.0.0.1` | |
| `SBI_IMAP_PORT` | `1143` | Proton Mail Bridge's default. Hosted servers: 993 with `tls`, or 143 |
| `SBI_IMAP_SECURITY` | `starttls` | `starttls` (upgrade before anything is sent) or `tls` (from the first byte). There is no unencrypted mode |
| `SBI_IMAP_USERNAME` | none | For Bridge, the Proton address |
| `SBI_IMAP_PASSWORD` | none | For Bridge, the password **Bridge** shows in its window, not the Proton password |
| `SBI_IMAP_TLS_CERT` | none | A PEM file holding the one certificate to trust, instead of the system's certificate authorities. A full path |
| `SBI_IMAP_TIMEOUT` | `30` | Seconds to wait for the mail server |

The certificate is always verified, and its name checked against `SBI_IMAP_HOST`. With
`SBI_IMAP_TLS_CERT` unset that is against the system's certificate authorities, which is right for
a hosted server. Proton Mail Bridge presents a certificate it made itself, so name a file and let
`second-brain-imap pin-cert` write Bridge's certificate into it
([proton-mail-bridge.md](proton-mail-bridge.md#the-certificate)). `pin-cert` only does that for a
mail server on this machine: across a network, the first certificate that arrives could be
anybody's.

The server starts without a username or password, and without the mail server running; each call
then fails with a sentence saying so. That lets it start at login beside a Bridge that is still
coming up.

## Folders

Three settings decide what a client can reach. Names are mailbox names exactly as
`second-brain-imap folders` prints them (`INBOX` in any letter case), comma-separated. Each entry
covers the mailbox and everything under it.

| Variable | Default | |
|---|---|---|
| `SBI_FOLDERS` | unset: every mailbox | **Only these can be opened.** |
| `SBI_EXCLUDE_FOLDERS` | unset | **These are hidden**, and their messages are withheld everywhere |
| `SBI_HIDE_SPAM_AND_TRASH` | `true` | Hides Spam and Trash the same way. Only `false`, `no`, `off` or `0` turns it off |

Spam and Trash are the mailboxes the mail server flags as such (Bridge does). On a server that
flags nothing they are found by their usual names (`Spam`, `Junk`, `Trash`, `Deleted Items` and
the like). If neither works, nothing is guessed: the server is refused until you name the
mailboxes in `SBI_EXCLUDE_FOLDERS` or set the switch to `false`.

The two lists do different jobs:

- A mailbox left out of `SBI_FOLDERS` is **closed**: it can't be listed, searched or read from.
  Nothing more.
- A mailbox in `SBI_EXCLUDE_FOLDERS` is **hidden**: closed, and every message in it is also
  withheld wherever else the mail server shows it. Mail servers show one message in several
  places. With Bridge, a message in Trash is also in All Mail and under each of its labels; hiding
  only the Trash mailbox would leave it readable there. Excluding a label hides everything that
  carries that label, wherever it is filed.

`second-brain-imap folders` marks each mailbox `visible`, `closed` or `hidden` (`-` for a
container that holds no mail itself).

**The settings fail closed.** These are refused, on the command line and on every tool call,
rather than resolved by guessing:

- a name that matches no mailbox (a typo, or a mailbox renamed since);
- a variable that is set but lists nothing;
- a mailbox in both lists;
- the view of all mail (Bridge's `All Mail`) in `SBI_EXCLUDE_FOLDERS`: every message is in it, so
  that would hide everything. To keep it closed, leave it out of `SBI_FOLDERS`;
- Spam or Trash in `SBI_FOLDERS` while `SBI_HIDE_SPAM_AND_TRASH` is on;
- the switch on, and no mailbox recognisable as Spam or as Trash (above);
- settings that leave nothing visible;
- `SBI_HIDE_SPAM_AND_TRASH` set to something that is neither a yes nor a no (the protection stays
  on; the commands refuse to run so that you find out).

A remote client is told only that the folder settings need fixing. Which entry is wrong goes to
the server's log and the command line, because an entry can be the name of something you meant to
hide.

Things to know:

- Message and unread counts in `mail_list_folders` are the mail server's own for each mailbox.
  All Mail's, and a label's, include hidden messages that are in them.
- Excluding a view or a label hides the messages in it everywhere: excluding `Starred` withholds
  every starred message, in the inbox too.
- A mailbox whose name contains a comma can't be listed in these variables.
- With `SBI_FOLDERS` set and the all-mail view not in it, a search of everything goes mailbox by
  mailbox and shows a message that is in several of them once.

## HTTP mode

For a Claude custom connector: [remote-access.md](remote-access.md) explains these.

| Variable | Default | |
|---|---|---|
| `SBI_TRANSPORT` | `stdio` | `stdio` or `http` |
| `SBI_HOST` | `127.0.0.1` | Address to bind |
| `SBI_PORT` | `8776` | |
| `SBI_HTTP_PATH` | `/mcp` | |
| `SBI_PUBLIC_URL` | none | The server's public origin, `https://host` |
| `SBI_RATE_LIMIT_PER_MINUTE` | `60` | Tool calls per caller; `0` is off |
| `SBI_GITHUB_CLIENT_ID` | none | The GitHub OAuth App |
| `SBI_GITHUB_CLIENT_SECRET` | none | |
| `SBI_JWT_SIGNING_KEY` | none | 32 characters or more; changing it signs every client out |
| `SBI_ALLOWED_GITHUB_IDS` | none | Numeric GitHub user ids that may sign in |
| `SBI_OAUTH_REDIRECT_URIS` | Claude's | Which clients may register; leave unset for Claude |
