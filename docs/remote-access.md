# Remote access

`second-brain-imap` normally runs over **stdio**, started by an MCP client on the same machine as
the mail server. Claude's **custom connectors** (claude.ai, the desktop and mobile apps, Claude
Code) work differently: they call an MCP server over HTTPS **from Anthropic's cloud**, not from
your device. HTTP mode lets this server be one of those connectors.

```
Claude (Anthropic's cloud)
        │  HTTPS, OAuth bearer token
        ▼
your tunnel or reverse proxy (public URL, TLS)
        │
        ▼
second-brain-imap serve --transport http   (127.0.0.1:8776)
        │  IMAP over TLS, never leaves the machine
        ▼
the mail server (Proton Mail Bridge on 127.0.0.1:1143)
```

The server stays bound to loopback. Something else publishes it: a Cloudflare Tunnel, Tailscale
Funnel, or a reverse proxy with TLS. The mailbox password never leaves the machine.

This is the same design as
[second-brain-joplin's](https://github.com/second-brain-tools/second-brain-joplin/blob/main/docs/remote-access.md),
whose guide goes further into publishing and keeping a server running. What differs is below.

## Running in HTTP mode

```bash
second-brain-imap serve --transport http --public-url https://mail.example.com --env-file serve.env
```

| Flag | Variable | Default | |
|---|---|---|---|
| `--transport {stdio,http}` | `SBI_TRANSPORT` | `stdio` | |
| `--host` | `SBI_HOST` | `127.0.0.1` | Loopback; the tunnel reaches it locally |
| `--port` | `SBI_PORT` | `8776` | One above second-brain-joplin's, so both can run on one machine |
| `--path` | `SBI_HTTP_PATH` | `/mcp` | The MCP endpoint |
| `--public-url` | `SBI_PUBLIC_URL` | none | The public origin (`https://host`, no path); sign-in needs it |
| `--env-file PATH` | | none | `KEY=value` lines, loaded first; variables already set win |
| `--no-auth` | | off | No sign-in, for a local test only |

- **Stateless, plain JSON.** Each request stands alone, so a restart never breaks a client's
  session and there is no idle stream for a proxy to cut.
- **Host check.** A request whose `Host` header isn't a loopback name or the public URL's host is
  answered `421`.
- **`/healthz`** answers `{"server": true, "mail": true}` with `200`, or `"mail": false` with
  `503` when the mail server is down, refuses the sign-in, or a setting (`SBI_IMAP_PORT`,
  `SBI_IMAP_TIMEOUT`, `SBI_IMAP_SECURITY`, `SBI_HIDE_SPAM_AND_TRASH`) can't be read. It is never
  behind sign-in, so it
  carries nothing else. It signs in to the mail server at most once every ten seconds, however
  often it is asked. It proves that the server runs and that the mail server takes the sign-in.
  It does not prove that Bridge has finished syncing (a freshly signed-in Bridge answers before
  its folders are complete), nor that a given folder holds all its mail.

### What it refuses to start with

- HTTP mode without sign-in configured, unless `--no-auth` is given.
- `--no-auth` on anything but a loopback address, or together with a public URL. A tunnel reaches
  a loopback server as easily as a public one, so loopback alone is not protection.
- Sign-in without a public URL, a public URL that isn't `https` (off loopback), or a signing key
  under 32 characters.
- Folder settings the mailbox doesn't match ([configuration.md](configuration.md#folders)), when
  the mail server answers. When it doesn't, the server starts and every call fails closed until
  it does: at login the mail server may come up second.

## Sign-in

Claude signs in with OAuth. GitHub is the identity: the server sends you to GitHub, GitHub says
who you are, and the server lets in only the numeric GitHub user ids you listed. Ids, not logins:
a login can be renamed and re-registered by somebody else.

1. **Create a GitHub OAuth App** (GitHub → Settings → Developer settings → OAuth Apps):
   homepage `https://mail.example.com`, callback `https://mail.example.com/auth/callback`.
   One app per server: don't reuse another connector's.
2. **Find your id:** `curl -s https://api.github.com/users/<login> | grep '"id"'`.
3. **Make a signing key:** `openssl rand -base64 32`.
4. Put them in the env file:

   ```
   SBI_PUBLIC_URL=https://mail.example.com
   SBI_GITHUB_CLIENT_ID=...
   SBI_GITHUB_CLIENT_SECRET=...
   SBI_JWT_SIGNING_KEY=...
   SBI_ALLOWED_GITHUB_IDS=12345
   ```

The server asks GitHub for `read:user` only. An account that isn't listed is refused before any
MCP message is read, and a second check runs before every tool. Only Claude's own callback may
register as a client unless `SBI_OAUTH_REDIRECT_URIS` says otherwise.

**Mail is more sensitive than most things a connector reaches, so there is no way to publish this
server without sign-in.**

### Turning access off

Any one of these: revoke the OAuth App's grant in your GitHub settings; change
`SBI_JWT_SIGNING_KEY` and restart (every client must sign in again); stop the tunnel.

## Publishing it

Put the server behind HTTPS at the public URL. With a Cloudflare Tunnel that is one hostname
routed to `http://127.0.0.1:8776`.

**A firewall in front is worth having.** Claude's connector traffic comes from Anthropic's
published outbound addresses (<https://platform.claude.com/docs/en/api/ip-addresses>), on every
Claude surface. So the whole hostname can be closed to everyone else, except the three pages your
own browser opens while signing in: `/authorize`, `/auth/callback` and `/consent`. Rate-limit
those three.

Then in Claude: Customize → Connectors → add a custom connector with the URL
`https://mail.example.com/mcp`, and sign in. After a change that adds or removes a tool, use
"Refresh tools list" on the connector: a new chat alone keeps the old list.

## Limits and logs

- Each caller gets `SBI_RATE_LIMIT_PER_MINUTE` tool calls a minute (60), with bursts of half.
- A tool answer is bounded: 50 results, or 100,000 characters of one message
  ([tools.md](tools.md)).
- The log has one line per tool call:
  `tool=mail_search caller=<github id> client=Claude outcome=ok ms=212`. A failure names its cause
  by class (`outcome=error:MailNotFoundError`). `client=` is what the client says about itself
  (its sign-in registration, its handshake, or its `User-Agent`; else `unknown`), cut to 40 safe
  characters, and proves nothing. It names a sign-in, not an app: the Claude apps that use your
  account's connector (Claude.ai and Claude Code among them) sign in through it once, as `Claude`,
  so they all show `client=Claude`. Never arguments (a search term
  says what you are looking for), results, addresses or credentials. The web server's access log is off, because the sign-in callback's address carries
  a code. Libraries that print mail at debug level are held above it.

## Before you connect it: what a model that reads your mail can be told

Anyone can send you mail, and the model reads what they wrote. This server removes text you could
not have seen yourself and labels the rest as information, but a message can still simply ask, in
plain words, for something to be done. What limits the damage is what else the same chat can do:

- This server can't send, delete or change mail, so mail can't be used against the mailbox.
- A chat that also has tools which write or share things (notes, files, calendars, a browser)
  gives a hostile message somewhere to act. Prefer approval prompts on those tools in chats that
  read mail.
