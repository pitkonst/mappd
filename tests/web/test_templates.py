from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from mappd.web.templates import templates_for

if TYPE_CHECKING:
    from pathlib import Path


def test_process_templates_extend_the_core_layout(tmp_path: Path) -> None:
    (tmp_path / "hello.html").write_text(
        '{% extends "layout.html" %}{% block content %}<p>hi {{ who }}</p>{% endblock %}'
    )
    templates = templates_for(tmp_path)
    app = FastAPI()
    app.state.processes = ["support"]

    @app.get("/", response_class=HTMLResponse)
    async def page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "hello.html", {"who": "Anna"})

    html = TestClient(app).get("/").text
    assert "<p>hi Anna</p>" in html
    assert '<a href="/support">support</a>' in html
    assert "/static/htmx.min.js" in html
    assert "/static/live.js" in html


def test_live_script_is_served() -> None:
    from mappd.web import STATIC  # noqa: PLC0415

    script = (STATIC / "live.js").read_text()
    assert "visibilitychange" in script, "hidden tabs give their stream back"


def test_times_are_shown_in_utc_and_say_so() -> None:
    from datetime import UTC, datetime, timedelta, timezone  # noqa: PLC0415

    from mappd.web.templates import utc  # noqa: PLC0415

    berlin = datetime(2026, 10, 23, 16, 3, tzinfo=timezone(timedelta(hours=2)))
    assert utc(berlin) == "2026-10-23 14:03 UTC"
    assert utc(datetime(2026, 10, 23, 14, 3)) == "2026-10-23 14:03 UTC", "naive is UTC"
    assert utc(datetime(2026, 10, 23, 14, 3, tzinfo=UTC), "%Y-%m-%d", label=False) == "2026-10-23"
    assert utc(None) == ""


def test_brief_reads_like_words() -> None:
    from mappd.web.templates import brief  # noqa: PLC0415

    assert brief({"sku": "CTL-H400", "on_hand": 4}) == "sku: CTL-H400 · on hand: 4"
    assert brief({"skus": ["CTL-H400", "CAB-W60"]}) == "skus: CTL-H400, CAB-W60"
    assert brief(None) == ""


def test_workflow_ids_read_with_their_colons() -> None:
    from mappd.web.templates import readable_id  # noqa: PLC0415

    assert readable_id("webhook:support%3Amail-1%3Aclosed") == "webhook:support:mail-1:closed"
    assert readable_id("a%2525b") == "a%25b"
