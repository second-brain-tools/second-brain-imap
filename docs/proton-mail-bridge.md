# With Proton Mail Bridge

[Proton Mail Bridge](https://proton.me/mail/bridge) is Proton's own app. It signs in to your
Proton account, decrypts your mail on the machine it runs on, and serves it as IMAP and SMTP on
`127.0.0.1` only. It needs a paid Proton plan. second-brain-imap runs beside it and talks to its
IMAP port; nothing of Bridge is exposed to anything else.

```
Proton  ⇄  Proton Mail Bridge (127.0.0.1:1143, IMAP over STARTTLS)  ⇄  second-brain-imap  ⇄  Claude
```

What follows was observed with Bridge 3.27.2 on macOS.

## Sign-in

Bridge shows, for each account, the details a mail client needs: under the account's mailbox
details, a **username** (your Proton address) and a **password** that Bridge generated. That
password, not your Proton password, is `SBI_IMAP_PASSWORD`. It works only against Bridge on that
machine. If you reset Bridge or it ever shows a different one, update the setting.

Bridge listens as soon as it runs, before any account is signed in, and while its first
synchronisation is still going. During that first run the mailbox is simply incomplete.

## The certificate

Bridge encrypts the connection with a certificate it makes for itself on first start (for the
name `127.0.0.1`, valid 20 years). No certificate authority vouches for it, so a client has to be
told to trust that one certificate. Name a file and save it once:

```bash
# in the env file:  SBI_IMAP_TLS_CERT=/full/path/to/imap-cert.pem
second-brain-imap pin-cert --env-file /path/to/serve.env
```

It prints the certificate's SHA-256 fingerprint. `pin-cert` takes the certificate from whatever
answers on Bridge's port at that moment, which is why it only does this for a mail server on the
same machine. To be sure it was Bridge, compare the fingerprint with Bridge's own copy: its
settings can export its TLS certificate, and
`openssl x509 -in cert.pem -noout -fingerprint -sha256` prints that file's fingerprint. From then
on that certificate is the only one trusted, and the name is still checked. If Bridge is reinstalled or reset it makes a new
certificate; `pin-cert` then refuses, showing both fingerprints, until you run it with
`--replace`.

## How Bridge lays out your mail

| Mailbox | |
|---|---|
| `INBOX`, `Sent`, `Drafts`, `Archive`, `Spam`, `Trash` | Proton's system folders. A message is in exactly one folder |
| `Folders/<name>` | Your folders |
| `Labels/<name>` | Your labels. A labelled message is here **as well as** in its folder |
| `Starred` | A view of the starred messages |
| `All Mail` | A view of everything, **Spam and Trash included** |

`Folders` and `Labels` themselves hold no mail.

So one message can be in three or four mailboxes at once. That is why hiding a mailbox here also
withholds its messages everywhere else ([configuration.md](configuration.md#folders)): with the
default settings a message in Trash can't be found through All Mail or through a label.
second-brain-imap matches the copies by the id Bridge stamps on every message
(`X-Pm-Internal-Id`).

A search without a folder runs once, in All Mail.

## What Bridge's search misses

Bridge answers searches itself, from its decrypted copy, by looking for the bytes of your term in
each message as stored. This comes from reading Bridge's source, not from measuring it here. Two
consequences:

- **A word with an accent or in a non-Latin script may not be found** in a message body, and in a
  subject or sender name that was sent in encoded form. Bridge compares raw bytes and most such
  text is stored encoded.
- **A word that falls where a long line was wrapped for storage can be missed** in a body.

Sender addresses, plain-ASCII subjects and dates are reliable. When a body search finds nothing,
try the sender or the subject and a date range instead.

## Things that go wrong

| What you see | Why |
|---|---|
| `check`: the certificate file can't be read | `SBI_IMAP_TLS_CERT` names a file that isn't there yet: run `pin-cert` |
| `check`: the mail server isn't answering, with `SSLCertVerificationError` | Bridge has a new certificate (reinstalled or reset): `pin-cert --replace` |
| `check`: the mail server isn't answering, with `ConnectionRefusedError` | Bridge isn't running, or uses another port (its Settings show the ports) |
| `check`: the mail server refused the sign-in | No account is signed in to Bridge, or the Bridge password changed |
| A tool says a message id is out of date | Bridge rebuilt its mailbox; search again |
| After a restart of the machine nothing answers | Bridge starts when you log in to the Mac, and keeps its key in your login keychain. Until you log in it isn't running |

Bridge updates itself, and Proton stops serving versions that fall too far behind, so leave its
automatic updates on.
