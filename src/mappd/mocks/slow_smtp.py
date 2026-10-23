"""An SMTP relay that holds back its final reply: the dangerous window, for acceptance tests.

It accepts any login, forwards each message to the upstream server (GreenMail in the stack),
and only then answers `250` — after `MAPPD_SLOW_SMTP_DELAY` seconds (default 5) when a
recipient's local part starts with `slow`. Killing the app in that pause leaves a message
delivered but never acknowledged, which is what `mappd.send_email` must settle as `unknown`.
Python's standard library only.

Run with: python -m mappd.mocks.slow_smtp (listens on MAPPD_SLOW_SMTP_PORT, default 2525).
"""

import asyncio
import os
import smtplib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

CRLF = b"\r\n"


class Relay:
    """One relay; with `upstream=None` it keeps what it receives instead (a sink for tests)."""

    def __init__(
        self,
        upstream: tuple[str, int] | None,
        *,
        delay: float = 5.0,
        user: str = "",
        password: str = "",
    ) -> None:
        self.upstream = upstream
        self.delay = delay
        self.user = user
        self.password = password
        self.received: list[tuple[str, list[str], bytes]] = []

    def _forward(self, sender: str, recipients: list[str], data: bytes) -> None:
        if self.upstream is None:
            self.received.append((sender, recipients, data))
            return
        host, port = self.upstream
        with smtplib.SMTP(host, port, local_hostname="slow-smtp", timeout=30) as client:
            if self.user:
                client.login(self.user, self.password)
            client.sendmail(sender, recipients, data)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One SMTP session."""

        async def say(line: str) -> None:
            writer.write(line.encode() + CRLF)
            await writer.drain()

        sender, recipients = "", list[str]()
        await say("220 mappd slow-smtp")
        try:
            while line := await reader.readline():
                text = line.decode(errors="replace").strip()
                verb, _, arg = text.partition(" ")
                verb = verb.upper()
                if verb == "EHLO":
                    await say("250-mappd slow-smtp\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME")
                elif verb in {"HELO", "NOOP"}:
                    await say("250 ok")
                elif verb == "AUTH":
                    await self._auth(arg, reader, say)
                elif verb == "MAIL":
                    sender, recipients = arg.partition(":")[2].split()[0].strip("<>"), []
                    await say("250 ok")
                elif verb == "RCPT":
                    recipients.append(arg.partition(":")[2].strip().strip("<>"))
                    await say("250 ok")
                elif verb == "DATA":
                    await say("354 end with <CRLF>.<CRLF>")
                    await say(await self._deliver(sender, recipients, await self._data(reader)))
                elif verb == "RSET":
                    sender, recipients = "", []
                    await say("250 ok")
                elif verb == "QUIT":
                    await say("221 bye")
                    break
                else:
                    await say("502 not implemented")
        finally:
            writer.close()

    async def _deliver(self, sender: str, recipients: list[str], data: bytes) -> str:
        """Forward the message, wait when a recipient is `slow*`; the final reply."""
        try:
            await asyncio.to_thread(self._forward, sender, recipients, data)
        except (OSError, smtplib.SMTPException) as e:
            return f"451 upstream failed: {type(e).__name__}"
        if any(r.partition("@")[0].lower().startswith("slow") for r in recipients):
            await asyncio.sleep(self.delay)
        return "250 queued"

    @staticmethod
    async def _auth(
        arg: str, reader: asyncio.StreamReader, say: Callable[[str], Awaitable[None]]
    ) -> None:
        """Accept any credentials, PLAIN (with or without an initial response) or LOGIN."""
        mechanism, _, initial = arg.partition(" ")
        if mechanism.upper() == "LOGIN":
            await say("334 VXNlcm5hbWU6")
            await reader.readline()
            await say("334 UGFzc3dvcmQ6")
            await reader.readline()
        elif not initial:
            await say("334 ")
            await reader.readline()
        await say("235 accepted")

    @staticmethod
    async def _data(reader: asyncio.StreamReader) -> bytes:
        lines = []
        while (line := await reader.readline()) not in (b".\r\n", b".\n", b""):
            lines.append(line[1:] if line.startswith(b"..") else line)
        return b"".join(lines)


async def main() -> None:
    """Serve on MAPPD_SLOW_SMTP_PORT, forwarding to MAPPD_SLOW_SMTP_UPSTREAM (host:port)."""
    host, _, port = os.environ.get("MAPPD_SLOW_SMTP_UPSTREAM", "greenmail:3025").partition(":")
    relay = Relay(
        (host, int(port or 25)),
        delay=float(os.environ.get("MAPPD_SLOW_SMTP_DELAY", "5")),
        user=os.environ.get("MAPPD_SLOW_SMTP_UPSTREAM_USER", ""),
        password=os.environ.get("MAPPD_SLOW_SMTP_UPSTREAM_PASSWORD", ""),
    )
    listen = int(os.environ.get("MAPPD_SLOW_SMTP_PORT", "2525"))
    server = await asyncio.start_server(relay.handle, "0.0.0.0", listen)  # noqa: S104 - a container
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
