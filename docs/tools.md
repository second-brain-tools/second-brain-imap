# Tools

Four tools, all read-only. Every answer that carries text from a message starts with a `notice`
saying that sender names, subjects, attachment names and message text come from whoever sent the
mail and are information, never instructions.

## mail_list_folders

No arguments. Returns the mailboxes a client may open:

| Field | |
|---|---|
| `name` | The name to pass as `folder` elsewhere, e.g. `INBOX`, `Folders/Receipts`, `Labels/Todo` |
| `role` | `inbox`, `sent`, `drafts`, `archive`, `spam`, `trash`, `starred`, `all`, or null |
| `messages`, `unread` | The mail server's own counts for that mailbox |

Hidden and closed mailboxes are not listed ([configuration.md](configuration.md#folders)).

## mail_search

Newest first. Every argument given must match.

| Argument | Default | |
|---|---|---|
| `text` | | A word or phrase anywhere in the message |
| `sender` | | In the From header |
| `recipient` | | In To or Cc |
| `subject` | | In the subject |
| `since` | | `YYYY-MM-DD`, the first day included |
| `before` | | `YYYY-MM-DD`, the first day left out |
| `folder` | everything | A name from `mail_list_folders` |
| `unread_only` | `false` | |
| `limit` | `20` | 1 to 50 |

Text arguments are at most 200 characters and match as written, in any letter case. There is no
`OR`, wildcard or fuzzy matching: this is the mail server's own search. Dates are the day the mail
server received the message. With no argument at all it lists the newest mail.

Without `folder`, the search runs in the server's all-mail view when there is one and the client
may open it, and otherwise in each visible mailbox in turn.

Returns:

| Field | |
|---|---|
| `total` | How many messages matched |
| `more` | True when there are matches beyond the ones returned |
| `messages` | Each with `id`, `date` (UTC), `sender`, `to`, `subject`, `folder`, `unread`, `starred` |

A very broad search looks at the newest 2,000 matches only; `total` then counts those (less any
that are hidden) and `more` is true. Narrow it with dates or another term.

## mail_recent

The mail that arrived in one mailbox in the last days, newest first.

| Argument | Default | |
|---|---|---|
| `folder` | `INBOX` | One mailbox, by name |
| `days` | `7` | 1 to 365 |
| `unread_only` | `false` | |
| `limit` | `20` | 1 to 50 |

Returns what `mail_search` returns.

## mail_read

One message as plain text.

| Argument | Default | |
|---|---|---|
| `message_id` | | The `id` of a result from `mail_search` or `mail_recent` |
| `offset` | `0` | Where in the text to start |
| `max_chars` | `50000` | 1 to 100,000 |

Returns `date`, `sender`, `to`, `cc`, `more_recipients` (how many beyond the first 20 of each),
`subject`, `folder`, `unread`, `starred`, and:

| Field | |
|---|---|
| `body` | Up to `max_chars` characters of the text, from `offset` |
| `total_chars` | The length of the whole text |
| `next_offset` | Where to continue, or null at the end |
| `body_truncated` | True when the message's text was over 4 MB as stored and was cut |
| `attachments` | Each with `name`, `type` and approximate `size_bytes`. Never their content |

What the text is:

- The form of the message a person is shown. Where it comes as plain text and as HTML, that is the
  HTML, turned into text; the plain form is used when there is no HTML. A message with several
  text parts that are shown one after another (a body, and the footer a mailing list added) is
  read as all of them in order.
- From HTML: what a person reading the mail would see. Elements styled to be invisible, scripts,
  styles and comments are dropped ([SECURITY.md](../SECURITY.md#mail-text-is-treated-as-hostile)
  says what is and isn't caught). A link keeps its words, followed by its address in angle
  brackets when that is an http, https or mailto address of 300 characters or fewer. An image
  becomes `[image: its alt text]`, or nothing. Nothing is fetched.
- With invisible characters removed: zero-width and direction-changing characters, Unicode tag
  characters, fillers, variation selectors and private-use characters.

Reading does not mark the message as read. An attached message (a forwarded `.eml`) is listed as
an attachment and not opened.

### About ids

An `id` names one copy of a message in one mailbox. It stays valid until the mail server rebuilds
that mailbox (for Bridge: after a reset, or removing and re-adding the account). It is then
refused with a sentence saying to search again. An id for a message that doesn't exist, or that
the folder settings hide, gets the same answer as any unknown id.
