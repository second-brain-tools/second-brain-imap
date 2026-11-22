"""A scripted IMAP server on a socket pair, driven by the real imapclient.IMAPClient.

tests/fakes.py stands in for the IMAP library, so it can't say what the library
puts on the wire: it accepted a form of search criteria that Proton Mail Bridge
rejects. Here the library is real and the server is the stand-in. The two ends
are an AF_UNIX socket pair inside the test process (no network), the server
frames commands the way an IMAP server does (lines, and ``{n}`` literals it
asks for), can start TLS with a certificate made for the test, and records
every command with whether it arrived encrypted.
"""

import datetime
import imaplib
import ipaddress
import re
import socket
import ssl
import threading
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from imapclient import IMAPClient
from imapclient import tls as imap_tls

FOLDERS = (
    (b"(\\Marked \\Noinferiors)", b"INBOX"),
    (b"(\\Marked \\Noinferiors \\Trash)", b"Trash"),
    (b"(\\Noinferiors \\Junk)", b"Spam"),
    (b"(\\All \\Marked)", b'"All Mail"'),
)
BODY = b"Plain body.\r\n"
STRUCTURE = b'("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 13 1 NIL NIL NIL NIL)'
LITERAL = re.compile(rb"\{(\d+)(\+?)\}\r\n$")
TIMEOUT = 5.0


def make_certificate(directory: Path, name: str, host: str = "127.0.0.1") -> tuple[str, str]:
    """A self-signed certificate for ``host`` and its key, as files; the way Bridge's is made."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / f"{name}.pem", directory / f"{name}.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


class Scripted(threading.Thread):
    """The server end of one connection."""

    def __init__(
        self,
        sock: socket.socket,
        *,
        certificate: tuple[str, str] | None = None,
        implicit_tls: bool = False,
        offer_starttls: bool = True,
        starttls_answer: bytes = b"OK",
    ) -> None:
        super().__init__(daemon=True)
        self.sock = sock
        self.certificate = certificate
        self.implicit_tls = implicit_tls
        self.offer_starttls = offer_starttls
        self.starttls_answer = starttls_answer
        self.buffer = b""
        self.encrypted = False
        # Every complete command, literals included, and whether it came through TLS.
        self.commands: list[tuple[bool, bytes]] = []
        self.selected = b""

    def _wrap(self) -> bool:
        assert self.certificate is not None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*self.certificate)
        try:
            self.sock = context.wrap_socket(self.sock, server_side=True)
        except (ssl.SSLError, OSError):
            return False  # the client refused the certificate, or hung up
        self.encrypted = True
        return True

    def _fill(self) -> bool:
        try:
            data = self.sock.recv(65536)
        except OSError:
            return False
        self.buffer += data
        return bool(data)

    def _line(self) -> bytes | None:
        while b"\r\n" not in self.buffer:
            if not self._fill():
                return None
        line, _, self.buffer = self.buffer.partition(b"\r\n")
        return line + b"\r\n"

    def _command(self) -> bytes | None:
        line = self._line()
        whole = line
        while line is not None and whole is not None:
            literal = LITERAL.search(line)
            if literal is None:
                return whole
            if not literal.group(2):
                self.sock.sendall(b"+ Ready for literal\r\n")
            size = int(literal.group(1))
            while len(self.buffer) < size:
                if not self._fill():
                    return None
            data, self.buffer = self.buffer[:size], self.buffer[size:]
            line = self._line()
            whole = whole + data + (line or b"")
        return None

    def run(self) -> None:
        if self.implicit_tls and not self._wrap():
            self.sock.close()
            return
        try:
            self._serve()
        except OSError:
            pass  # the client went away mid-answer
        finally:
            self.sock.close()

    def _serve(self) -> None:
        def send(data: bytes) -> None:
            self.sock.sendall(data)

        send(b"* OK scripted server ready\r\n")
        while (command := self._command()) is not None:
            self.commands.append((self.encrypted, command))
            tag, _, rest = command.partition(b" ")
            upper = rest.upper()
            if upper.startswith(b"CAPABILITY"):
                offer = b" STARTTLS" if self.offer_starttls and not self.encrypted else b""
                send(b"* CAPABILITY IMAP4rev1" + offer + b"\r\n" + tag + b" OK done\r\n")
            elif upper.startswith(b"STARTTLS"):
                if self.starttls_answer != b"OK":
                    send(tag + b" " + self.starttls_answer + b" no TLS here\r\n")
                    continue
                send(tag + b" OK begin TLS\r\n")
                if not self._wrap():
                    return
            elif upper.startswith(b"LOGIN"):
                send(tag + b" OK logged in\r\n")
            elif upper.startswith(b"LIST"):
                for flags, name in FOLDERS:
                    send(b"* LIST " + flags + b' "/" ' + name + b"\r\n")
                send(tag + b" OK done\r\n")
            elif upper.startswith(b"STATUS"):
                name = rest.split(b" (")[0][len(b"STATUS ") :]
                send(b"* STATUS " + name + b" (MESSAGES 1 UNSEEN 0 UIDNEXT 2 UIDVALIDITY 7)\r\n")
                send(tag + b" OK done\r\n")
            elif upper.startswith(b"EXAMINE"):
                self.selected = rest
                send(b"* 1 EXISTS\r\n* 0 RECENT\r\n* FLAGS (\\Seen)\r\n* OK [UIDVALIDITY 7] ok\r\n")
                send(b"* OK [UIDNEXT 2] ok\r\n" + tag + b" OK [READ-ONLY] examine done\r\n")
            elif upper.startswith(b"UID SEARCH"):
                send(b"* SEARCH 1\r\n" + tag + b" OK done\r\n")
            elif upper.startswith(b"UID FETCH"):
                send(
                    b"* 1 FETCH (" + b" ".join(self._items(rest)) + b")\r\n" + tag + b" OK done\r\n"
                )
            elif upper.startswith(b"LOGOUT"):
                send(b"* BYE bye\r\n" + tag + b" OK done\r\n")
            else:
                send(tag + b" BAD the scripted server doesn't know that\r\n")

    def _items(self, rest: bytes) -> list[bytes]:
        upper = rest.upper()
        items = [b"UID 1"]
        if b"FLAGS" in upper:
            items.append(b"FLAGS (\\Seen)")
        if b"INTERNALDATE" in upper:
            items.append(b'INTERNALDATE "02-Nov-2026 10:00:00 +0000"')
        if b"BODYSTRUCTURE" in upper:
            items.append(b"BODYSTRUCTURE " + STRUCTURE)
        fields = re.search(rb"BODY\.PEEK\[(HEADER\.FIELDS \([^)]*\))\]", rest, re.IGNORECASE)
        if fields:
            # Each mailbox's message has its own id, so the one in Trash is not a
            # copy of the one in All Mail.
            mailbox = re.sub(rb"\W", b"", self.selected)
            headers = (
                b"Date: Mon, 02 Nov 2026 10:00:00 +0000\r\nFrom: Alice <alice@example.com>\r\n"
                b"To: me@example.com\r\nSubject: Hello\r\nX-Pm-Internal-Id: pm-" + mailbox
            ) + b"\r\n\r\n"
            items.append(b"BODY[" + fields.group(1) + b"] {%d}\r\n" % len(headers) + headers)
        part = re.search(rb"BODY\.PEEK\[(\d[\d.]*)\](<\d+)?", rest, re.IGNORECASE)
        if part:
            window = (part.group(2) + b">") if part.group(2) else b""
            items.append(b"BODY[" + part.group(1) + b"]" + window + b" {%d}\r\n" % len(BODY) + BODY)
        return items


class Wire:
    """Replaces ``imap.IMAPClient`` with the real client wired to a :class:`Scripted` server."""

    def __init__(self, **server_options: Any) -> None:
        self.options = server_options
        self.servers: list[Scripted] = []

    def client_class(self) -> type[IMAPClient]:
        wire = self

        class Client(IMAPClient):  # type: ignore[misc]
            def _create_IMAP4(self) -> Any:  # noqa: N802 - the library's name
                ours, theirs = socket.socketpair()
                ours.settimeout(TIMEOUT)
                server = Scripted(theirs, implicit_tls=bool(self.ssl), **wire.options)
                server.start()
                wire.servers.append(server)
                client = self

                class Connection(imaplib.IMAP4):
                    def _create_socket(self, timeout: float | None = None) -> socket.socket:
                        if client.ssl:
                            wrapped: socket.socket = imap_tls.wrap_socket(
                                ours, client.ssl_context, client.host
                            )
                            return wrapped
                        return ours

                return Connection(self.host, self.port, timeout=TIMEOUT)

        return Client

    def join(self) -> None:
        for server in self.servers:
            server.join(timeout=TIMEOUT)

    def commands(self) -> list[bytes]:
        self.join()
        return [command for server in self.servers for _, command in server.commands]

    def verbs(self) -> list[bytes]:
        """Each command without its tag, cut at the first argument: ``UID SEARCH``, ``LOGIN``."""
        found = []
        for command in self.commands():
            words = command.split(b" ", 3)
            found.append(b" ".join(words[1:3]) if words[1].upper() == b"UID" else words[1].strip())
        return found

    def plaintext(self) -> list[bytes]:
        """What reached a server before, or without, TLS."""
        self.join()
        return [
            command for server in self.servers for inside, command in server.commands if not inside
        ]
