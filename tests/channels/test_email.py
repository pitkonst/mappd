from datetime import UTC, datetime
from email import message_from_bytes, policy
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mappd.channels import email as mail

if TYPE_CHECKING:
    from email.message import Message

EML = Path(__file__).parent / "eml"


def raw(name: str) -> bytes:
    return (EML / name).read_bytes()


def headers(text: str) -> Message:
    data = (text.strip() + "\r\n\r\nbody\r\n").replace("\n", "\r\n").encode()
    return message_from_bytes(data, policy=policy.default)


def test_plain_message_parses() -> None:
    e = mail.parse(raw("plain.eml"))
    assert e.message_id == "<plain-1@example.com>"
    assert (e.sender, e.sender_name) == ("anna@example.com", "Anna Muster")
    assert e.to == ["support@mappd.test"]
    assert e.subject == "Sensor offline"
    assert "room 3 is offline" in e.text
    assert e.date == datetime(2026, 10, 16, 7, 30, tzinfo=UTC)
    assert (e.in_reply_to, e.references, e.attachments, e.automated) == (None, [], [], None)


def test_html_only_becomes_text() -> None:
    text = mail.parse(raw("html_only.eml")).text
    assert "alert" not in text
    assert "color" not in text
    assert "pair the hub & the sensor?" in text
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines == ["Hello team,", "How do I pair the hub & the sensor?", "Thanks"]


def test_plain_part_wins_over_html() -> None:
    assert mail.parse(raw("alternative.eml")).text.strip() == "Plain wins."


def test_attachment_is_listed_not_text() -> None:
    e = mail.parse(raw("attachment.eml"))
    (att,) = e.attachments
    assert (att.filename, att.content_type) == ("rfq.pdf", "application/pdf")
    assert att.size > 0
    assert "PDF" not in e.text
    assert e.text.strip() == "Please quote the items in the attached list."


def test_encoded_headers_decode() -> None:
    e = mail.parse(raw("encoded.eml"))
    assert e.subject == "Grüße aus Köln"
    assert e.sender_name == "Jürgen Müller"
    assert e.text.strip() == "Датчик не работает."


def test_an_unknown_charset_decodes_with_replacement() -> None:
    e = mail.parse(raw("badcharset.eml"))
    assert e.text.startswith("Hello ")
    assert "�" in e.text


def test_missing_message_id_gets_a_stable_hash() -> None:
    first, second = mail.parse(raw("no_id.eml")), mail.parse(raw("no_id.eml"))
    assert first.message_id == second.message_id
    assert first.message_id.startswith("<")
    assert first.message_id.endswith("@mappd.local>")


def test_message_ids_are_normalized() -> None:
    assert mail.normalize_id(" <A@B> ") == "<A@B>"
    assert mail.normalize_id("a@b") == "<a@b>"
    assert mail.normalize_id("   ") is None
    assert mail.id_list("<one@x>\r\n <two@x>") == ["<one@x>", "<two@x>"]
    assert mail.id_list(None) == []
    e = mail.parse(raw("reply.eml"))
    assert e.in_reply_to == "<0123456789abcdef0123456789abcdef@mappd.test>"
    assert e.references == [
        "<plain-1@example.com>",
        "<0123456789abcdef0123456789abcdef@mappd.test>",
    ]


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [("autoreply.eml", "auto-submitted"), ("bounce.eml", "daemon"), ("plain.eml", None)],
)
def test_automated_fixtures(fixture: str, reason: str | None) -> None:
    assert mail.parse(raw(fixture)).automated == reason


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("From: a@example.com\nAuto-Submitted: auto-generated", "auto-submitted"),
        ("From: a@example.com\nAuto-Submitted: no", None),
        ("From: a@example.com\nPrecedence: bulk", "precedence"),
        ("From: a@example.com\nPrecedence: list", "precedence"),
        ("From: a@example.com\nX-Autoreply: yes", "autoreply-header"),
        ("From: a@example.com\nX-Autorespond: yes", "autoreply-header"),
        ("From: postmaster@example.com", "daemon"),
        ("From: a@example.com\nContent-Type: multipart/report; boundary=x", "report"),
        ("From: Support <support@mappd.test>", "own"),
    ],
)
def test_automated_rules(text: str, reason: str | None) -> None:
    assert mail.automated(headers(text), "support@mappd.test") == reason


def test_oversize_headers_parse() -> None:
    head = raw("plain.eml").split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
    e = mail.parse_headers(head, 30_000_000)
    assert e.truncated is True
    assert e.text == "[message over 25 MB not downloaded]"
    assert e.message_id == "<plain-1@example.com>"


def test_reply_subject_never_doubles() -> None:
    assert mail.reply_subject("Re: RE: re: Hi") == "Re: Hi"
    assert mail.reply_subject("Hi") == "Re: Hi"
    assert mail.reply_subject("") == "Re:"


def context(text: str = "Line one\nLine two") -> mail.ReplyContext:
    return mail.ReplyContext(
        message_id="<orig@example.com>",
        references=["<root@example.com>"],
        subject="Sensor offline",
        sender="anna@example.com",
        date=datetime(2026, 10, 16, 7, 30, tzinfo=UTC),
        text=text,
    )


def test_build_threads_a_reply() -> None:
    m = mail.build(
        sender="support@mappd.test",
        to="anna@example.com",
        subject="Sensor offline",
        text="Please restart the hub.",
        message_id="<x@mappd.test>",
        reply_to=context(),
    )
    assert m["Subject"] == "Re: Sensor offline"
    assert m["In-Reply-To"] == "<orig@example.com>"
    assert m["References"] == "<root@example.com> <orig@example.com>"
    assert m["Message-ID"] == "<x@mappd.test>"
    assert (m["From"], m["To"]) == ("support@mappd.test", "anna@example.com")
    assert m["Date"]
    body = m.get_content()
    assert body.startswith("Please restart the hub.")
    assert "anna@example.com wrote:" in body
    assert "> Line one\n> Line two" in body


def test_quote_is_capped() -> None:
    quoted = mail.quote(context("\n".join(f"line {i}" for i in range(60))))
    assert len([line for line in quoted.splitlines() if line.startswith(">")]) == 50


def test_build_fresh_mail_has_no_thread_headers() -> None:
    m = mail.build(
        sender="support@mappd.test",
        to="anna@example.com",
        subject="Your ticket",
        text="Hello",
        message_id="<y@mappd.test>",
        reply_to=None,
    )
    assert m["Subject"] == "Your ticket"
    assert m["In-Reply-To"] is None
    assert m["References"] is None


def test_message_id_for_is_deterministic() -> None:
    a = mail.message_id_for("mappd.send_email:support:t-1", "support@mappd.test")
    assert a == mail.message_id_for("mappd.send_email:support:t-1", "Support <support@mappd.test>")
    assert a != mail.message_id_for("mappd.send_email:support:t-2", "support@mappd.test")
    assert a.endswith("@mappd.test>")
    assert len(a) == len("<@mappd.test>") + 32


def test_reply_context_of_an_email() -> None:
    e = mail.parse(raw("reply.eml"))
    ctx = mail.ReplyContext.of(e)
    assert ctx.message_id == "<reply-1@example.com>"
    assert ctx.references == e.references
    assert (ctx.subject, ctx.sender) == ("Re: Sensor offline", "anna@example.com")


def test_parse_never_raises_on_hostile_charsets() -> None:
    for charset in ("idna", "utf-8''b%00", "utf-8\x00x"):
        data = (
            "From: a@example.com\r\nMessage-ID: <c@x>\r\n"
            f'Content-Type: text/plain; charset="{charset}"\r\n\r\nhello\r\n'
        ).encode()
        assert "hello" in mail.parse(data).text


def test_parse_survives_a_forwarded_message_with_broken_headers() -> None:
    data = (
        b"From: a@example.com\r\nMessage-ID: <f@x>\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n--b\r\n'
        b"Content-Type: text/plain\r\n\r\nsee below\r\n--b\r\n"
        b"Content-Type: message/rfc822\r\n\r\n"
        b"To: \xff,\r\n :\\)-b)\r\nSubject: x\r\n\r\ninner\r\n--b--\r\n"
    )
    e = mail.parse(data)
    assert e.text == "see below"
    assert [a.content_type for a in e.attachments] == ["message/rfc822"]


def test_nul_bytes_are_removed() -> None:
    data = (
        b"From: a@example.com\r\nMessage-ID: <n@x>\r\nSubject: =?utf-8?q?a=00b?=\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nbo\x00dy\r\n"
    )
    e = mail.parse(data)
    assert (e.subject, e.text) == ("ab", "body")


def test_a_huge_message_id_is_hashed() -> None:
    data = f"From: a@example.com\r\nMessage-ID: <{'x' * 3000}@x>\r\n\r\nhi\r\n".encode()
    e = mail.parse(data)
    assert len(e.message_id) < 100
    assert e.message_id.endswith("@mappd.local>")
    assert e.message_id == mail.parse(data).message_id


def test_reply_to_is_parsed() -> None:
    data = (
        b"From: Contact form <noreply@forms.example>\r\nReply-To: Anna <anna@example.com>\r\n"
        b"Message-ID: <r@x>\r\n\r\nhi\r\n"
    )
    e = mail.parse(data)
    assert (e.sender, e.reply_to) == ("noreply@forms.example", "anna@example.com")
    assert mail.parse(raw("plain.eml")).reply_to is None
