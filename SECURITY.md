# Security

## Reporting a problem

Please report a suspected vulnerability privately, through GitHub Security Advisories
(**Security → Report a vulnerability** on the repository), rather than in a public issue. We aim
to acknowledge a report within a few days.

## What this server is trusted with

Read access to a whole mailbox, and the mailbox's IMAP password. What it must never do: change the
mailbox, show mail to anyone but the people allowed to sign in, show a message the operator hid,
or hand a model text that the mail's human reader could not have seen.

## What it does about each

### It can't change the mailbox

- After signing in it sends `LIST`, `STATUS`, `EXAMINE`, `UID SEARCH`, `UID FETCH` and `LOGOUT`
  (the IMAP library adds `CAPABILITY`, which asks what the server can do). One module,
  [imap.py](src/second_brain_imap/imap.py), is the only place a command leaves from, and it has
  no method that stores, appends, copies, moves, expunges, creates or deletes. One test reads
  that module's syntax tree and fails if it calls anything else on the IMAP client; another fails
  if any other module touches the IMAP library; a third drives the real IMAP library against a
  scripted server and checks the bytes that arrive.
- Mailboxes are opened with `EXAMINE`. A server that answers without confirming read-only is
  refused.
- Message data is fetched with `BODY.PEEK` only, which does not set `\Seen`. Any other fetch item
  is rejected before it is sent.
- What a caller asks to search for is sent as an IMAP literal (its length, then exactly that many
  bytes), never as part of the command line, so a term such as `{5}` or `a) OR (ALL` is text and
  nothing else. Only the search keys this server uses are accepted, and line breaks are removed
  from every term first.
- There is no SMTP code.

### Only listed people get in (HTTP mode)

GitHub sign-in limited to numeric GitHub user ids, checked when the token is verified and again
before each tool. No sign-in means no HTTP mode, except `--no-auth` on a loopback address with no
public URL. See [docs/remote-access.md](docs/remote-access.md).

### Hidden mail stays hidden

- Spam and Trash are hidden by default; so is every mailbox in `SBI_EXCLUDE_FOLDERS`. Spam and
  Trash are the mailboxes the server flags as such, or failing that the ones with a usual name
  for them. A server that does neither is refused until the mailboxes are named or the switch is
  turned off: "hidden by default" never quietly means "not hidden".
- A hidden message is withheld wherever else the mail server shows a copy of it. Copies are
  matched by Proton's message id where Bridge provides it. On other servers, by a digest of the
  Message-ID, date, sender and subject together: a Message-ID alone is public, and would let a
  sender pass a message off as another's copy.
- What is hidden is read **after** the messages to be shown have been collected, on every call. A
  message that lands in Spam while a call runs is either among what is hidden or not among what
  is shown.
- A hidden message that can't be identified (the server leaves out its headers, or a count)
  refuses the call. It is never skipped.
- A hidden mailbox, a mailbox that doesn't exist and a message that doesn't exist all get the same
  answer, so an answer never says that something hidden exists.
- Folder settings that don't resolve refuse every call. They never fall back to showing more.
- Known limits: a mailbox's message and unread counts are the mail server's own, so All Mail's,
  and a label's, include hidden messages that are in them. On a server without Proton's ids,
  someone who knows a message's Message-ID, date, sender and subject exactly can send junk that
  makes the real message be withheld; the matching errs toward hiding.

### Mail text is treated as hostile

- **The form a person sees.** Where a message comes as plain text and as HTML, mail clients show
  the HTML, so that is what is read; the plain form is used only when there is no HTML. Text put
  only in the form nobody is shown is not the message.
- **HTML is read the way a browser builds the page**, as far as that decides what is hidden: end
  tags that may be left out, end tags that reach nothing, table boundaries. Dropped: scripts,
  styles, comments, the head, and everything inside an element that carries `hidden` or whose own
  `style` hides it. That covers `display:none`, `visibility:hidden`, near-zero opacity, fonts of
  2 px or less, a box of no size that cuts off its content, text indented or positioned far off
  the page, clipping to nothing, scaling to zero, transparent colour, and the same written with
  CSS comments or escapes. Also dropped: a closed `<details>` (but not its summary), an unopened
  `<dialog>`, the fallback content of `<video>`, `<audio>` and `<canvas>`, and the alt text of a
  one-pixel image. Markup the parser can't get through yields a fixed sentence, never the raw
  text.
- **This part is best effort.** Not caught: text hidden by a rule in a `<style>` block or a
  class; text in the background's own colour; anything that depends on one mail client's
  behaviour. A link's address and an image's alt text are shown on purpose.
- Invisible characters are removed from every string taken from a message: zero-width and
  direction-changing characters, Unicode tag characters, fillers, variation selectors,
  private-use characters, noncharacters, control characters.
- Nothing is fetched: no images, no links, no attachments. An attachment is a name, a type and a
  size. The type is shown only when it is two short tokens (`application/pdf`); anything else
  reads `x-unknown`, because the sender writes it. A declared character set is honoured only when
  it is one mail is really written in.
- Every answer carrying message text says that the text is information, not instructions, and so
  do the server's instructions to the client.
- **A sender can't make it slow.** Headers and HTML are read in time proportional to their size,
  whatever they contain, and each header is bounded before it is looked at. (The standard
  library's structured header parser, and a naive HTML reader, take minutes on a few hundred
  kilobytes chosen for the purpose.)
- A message can still ask for something in plain, visible words. What a model does with that
  depends on the client and on what other tools the same chat has
  ([docs/remote-access.md](docs/remote-access.md#before-you-connect-it-what-a-model-that-reads-your-mail-can-be-told)).

### The connection to the mail server

Always TLS, with the certificate verified and its name checked, including on `127.0.0.1`. For a
self-signed certificate (Bridge) the operator pins that one certificate; `pin-cert` will only do
so for a server on the same machine. There is no setting that skips verification.

### Credentials and logs

- The IMAP password lives in the environment or a file the operator names. It is left out of the
  settings object's printed form.
- Logs hold one line per tool call (tool, caller id, client, outcome, duration) and start-up
  counts. The client's name is what the client says about itself: a hint, never trusted.
  Never arguments, results, addresses or credentials. Error sentences sent to a client name no
  mailbox, address or setting value; the detail goes to the operator's log.
- A failure nobody planned for (a bug, an answer from the mail server in a shape this code doesn't
  know) gives the client one fixed sentence, and the log the exception's type and the place it
  surfaced. Never the exception's own text, which could quote the mail being processed.
- An installed copy reads no `.env` from the working directory or anywhere above its install, and
  FastMCP's own `.env` reading is switched off, so a project's `.env` can't repoint the server or
  change its transport. With `--env-file`, that file is the whole configuration: a checkout's
  `.env` is not read as well.
