import contextlib
import re
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import quote
from uuid import uuid4

import dbos_async
import psycopg
import pytest
import stranded
from dbos import DBOS, SetWorkflowID
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mappd import telemetry, workflow_stats, workflows
from mappd.web import errors, workflows_api, workflows_pages
from mappd.workflow_view import ActionRef, Step

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mappd.settings import Settings


def make_client(settings: Settings) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    app.state.processes = []
    errors.install(app)
    app.include_router(workflows_api.router)
    app.include_router(workflows_pages.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def close_shared() -> Iterator[None]:
    """The app's lifespan closes these; the test apps run without it."""
    yield
    workflows.close_shared()
    workflow_stats.close()


@pytest.fixture
def client(dbos_runtime: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MAPPD_DATABASE_URL", dbos_runtime.database_url)
    return make_client(dbos_runtime)


def done(wid: str | None = None) -> str:
    wid = wid or f"done-{uuid4()}"
    with SetWorkflowID(wid):
        DBOS.start_workflow(stranded.flaky_workflow).get_result()
    return wid


def sent(auto: bool = False) -> str:
    wid = f"sender-{uuid4()}"
    with SetWorkflowID(wid):
        dbos_async.run(stranded.sender_workflow(f"run-{uuid4()}", auto))
    return wid


def page(wid: str, suffix: str = "") -> str:
    return f"/workflows/{quote(wid, safe='')}{suffix}"


def test_list_page_shows_tiles_that_link_to_filters(client: TestClient) -> None:
    done()
    stranded.fail()
    html = client.get("/workflows").text
    assert 'href="/workflows?status=ERROR&amp;window=24h"' in html
    assert 'href="/workflows?status=stranded&amp;window=24h"' in html
    assert 'href="/workflows?status=SUCCESS&amp;window=24h"' in html
    assert "test.flaky" in html
    week = client.get("/workflows", params={"window": "7d"}).text
    assert 'href="/workflows?status=ERROR&amp;window=7d"' in week


def test_list_filters_round_trip_in_the_query_string(client: TestClient) -> None:
    prefix = f"pages-{uuid4()}"
    wid = done(f"{prefix}-a")
    other = done()
    html = client.get("/workflows", params={"id_prefix": prefix, "status": "SUCCESS"}).text
    assert wid in html
    assert other not in html
    assert f'value="{prefix}"' in html, "the filter form keeps its values"
    assert "status=SUCCESS" in html, "live refresh keeps the filters"
    assert client.get("/workflows", params={"status": "FINE"}).status_code == 422


def test_live_partial_has_the_trigger_and_can_be_off(client: TestClient) -> None:
    live = client.get("/workflows").text
    assert 'hx-trigger="every 10s"' in live
    assert 'hx-get="/workflows/list?' in live
    still = client.get("/workflows", params={"live": "0"}).text
    assert 'hx-trigger="every 10s"' not in still
    partial = client.get("/workflows/list").text
    assert "<html" not in partial
    assert 'id="workflow-list"' in partial


def test_detail_shows_steps_kinds_and_bars(client: TestClient, dbos_runtime: Settings) -> None:
    wid = sent()
    html = client.get(page(wid)).text
    for name in ("test.sender.first", "test.sender.send", "test.sender.last"):
        assert name in html
    assert ">action<" in html
    assert "test.sender:run-" in html, "the action row shows its outbox key"
    assert html.count('class="bar"') == 3
    assert re.search(r'style="left: [0-9.]+%; width: [0-9.]+%"', html)
    assert 'href="/workflows/' + quote(wid, safe="") + '/trace"' in html


def test_detail_polls_only_while_unfinished(client: TestClient, dbos_runtime: Settings) -> None:
    assert 'hx-trigger="every 3s"' not in client.get(page(done())).text
    running = stranded.strand(dbos_runtime, "old")
    try:
        html = client.get(page(running)).text
        assert 'hx-trigger="every 3s"' in html
        assert 'hx-get="' + page(running, "/live") + '"' in html
        assert 'hx-disinherit="*"' in html, "cancel's confirmation keeps its own target"
        live = client.get(page(running, "/live")).text
        assert "<html" not in live
        assert 'hx-trigger="every 3s"' in live
    finally:
        workflows.cancel(dbos_runtime, running)


def test_an_enqueued_workflow_page_renders(client: TestClient, dbos_runtime: Settings) -> None:
    wid = f"parked-{uuid4()}"
    with workflows.client(dbos_runtime) as c:
        c.enqueue(
            {
                "workflow_name": "test.flaky",
                "queue_name": "test-unserved",
                "workflow_id": wid,
                "app_version": "test",
            }
        )
    try:
        r = client.get(page(wid))
        assert r.status_code == 200
        assert "ENQUEUED" in r.text
        assert "no steps recorded" in r.text
    finally:
        workflows.cancel(dbos_runtime, wid)


def test_buttons_follow_the_status(client: TestClient, dbos_runtime: Settings) -> None:
    def buttons(wid: str) -> set[str]:
        return set(re.findall(r'/confirm/(\w+)"', client.get(page(wid)).text))

    assert buttons(done()) == {"fork"}
    assert buttons(stranded.fail()) == {"retry", "fork"}
    running = stranded.strand(dbos_runtime, "old")
    try:
        assert buttons(running) == {"cancel", "recover"}
    finally:
        workflows.cancel(dbos_runtime, running)


def test_fork_confirmation_lists_steps_by_kind(client: TestClient) -> None:
    keyed = client.get(page(sent(), "/confirm/fork"), params={"step": 2}).text
    assert "will not resend (outbox: done)" in keyed
    assert "runs again" in keyed, "the last plain step"
    assert "test.sender.first" not in keyed, "steps before the fork point are copied"
    auto = client.get(page(sent(auto=True), "/confirm/fork"), params={"step": 2}).text
    assert "will send again" in auto
    assert 'name="start_step" value="2"' in auto
    assert '<option value="test"' in auto


def test_fork_post_redirects_to_the_new_workflow(client: TestClient) -> None:
    wid = done()
    r = client.post(page(wid, "/fork"), data={"start_step": "2", "to_version": "test"})
    assert r.status_code == 200
    target = r.headers["HX-Redirect"]
    [fork] = workflows.get(client.app.state.settings, wid).forks  # type: ignore[attr-defined]
    assert target == page(fork)


def test_a_stale_action_shows_a_banner(client: TestClient) -> None:
    r = client.post(page(done(), "/cancel"))
    assert r.status_code == 200
    assert "HX-Redirect" not in r.headers
    assert 'class="flash bad"' in r.text
    assert "nothing to cancel" in r.text
    assert "no workflow" in client.post(page("nope", "/cancel")).text


def test_cancel_post_redirects_to_the_workflow(client: TestClient, dbos_runtime: Settings) -> None:
    running = stranded.strand(dbos_runtime, "old")
    try:
        r = client.post(page(running, "/cancel"))
        assert r.headers["HX-Redirect"] == page(running)
    finally:
        with contextlib.suppress(workflows.WorkflowStateError):
            workflows.cancel(dbos_runtime, running)


def test_recover_post_moves_one_workflow(client: TestClient, dbos_runtime: Settings) -> None:
    running = stranded.strand(dbos_runtime, "old")
    r = client.post("/workflows/recover", data={"to_version": "test", "workflow_id": running})
    [moved] = workflows.get(dbos_runtime, running).forks
    assert r.headers["HX-Redirect"] == page(moved)
    bad = client.post("/workflows/recover", data={"to_version": "typo"})
    assert 'class="flash bad"' in bad.text


def test_payloads_off_hides_them_on_the_page(dbos_runtime: Settings) -> None:
    html = make_client(replace(dbos_runtime, workflows_payloads=False)).get(page(done())).text
    assert "hidden (MAPPD_WORKFLOWS_PAYLOADS=off)" in html


def test_workflow_text_is_escaped(client: TestClient) -> None:
    wid = done(f"<script>alert(1)</script>-{uuid4().hex[:6]}")
    html = client.get(page(wid)).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in client.get("/workflows").text


def test_no_browser_dialogs(client: TestClient) -> None:
    wid = stranded.fail()
    for url in ("/workflows", page(wid), page(wid, "/confirm/retry"), page(wid, "/confirm/fork")):
        html = client.get(url, params={"step": 1} if "fork" in url else None).text
        assert "confirm(" not in html
        assert "hx-confirm" not in html


def test_trace_link_redirects(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry, "find_trace", lambda _s, _w: "tr-5")
    r = client.get(page(done(), "/trace"), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("selectedEvaluationId=tr-5")


def test_header_links_workflows(client: TestClient) -> None:
    assert '<a href="/workflows">workflows</a>' in client.get("/workflows").text


def test_the_filter_form_defaults_are_accepted(client: TestClient) -> None:
    prefix = f"form-{uuid4()}"
    wid = done(f"{prefix}-a")
    form = {"window": "24h", "process": "", "status": "", "version": "", "id_prefix": prefix}
    r = client.get("/workflows", params=form)
    assert r.status_code == 200
    assert wid in r.text
    assert client.get("/workflows/list", params=form).status_code == 200


def _step(status: str | None, *, per_workflow: bool = False) -> Step:
    ref = (
        None
        if status is None
        else ActionRef(idempotency_key="k", kind="x.send", status=status, per_workflow=per_workflow)
    )
    return Step(
        function_id=1,
        name="x.send",
        kind="action",
        started_at=None,
        completed_at=None,
        duration_ms=None,
        error=None,
        child_workflow_id=None,
        output=None,
        outbox=ref,
    )


@pytest.mark.parametrize(
    ("status", "per_workflow", "expected"),
    [
        ("done", False, "will not resend (outbox: done)"),
        ("done", True, "will send again (key per workflow)"),
        ("failed", False, "will send again (outbox: failed)"),
        ("pending", False, "will send again (outbox: pending)"),
        ("sending", False, "will send again (outbox: sending)"),
        ("unknown", False, "stops: outcome unknown, settle it in /actions first"),
        (None, False, "will send (no outbox record)"),
    ],
)
def test_fork_effect_follows_the_outbox_status(
    status: str | None, per_workflow: bool, expected: str
) -> None:
    assert workflows_pages.effect(_step(status, per_workflow=per_workflow)) == expected


def test_tiles_and_list_share_the_window(client: TestClient, dbos_runtime: Settings) -> None:
    prefix = f"aged-{uuid4()}"
    wid = done(f"{prefix}-a")
    with psycopg.connect(dbos_runtime.system_database_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE dbos.workflow_status SET created_at = created_at - 7200000"
            " WHERE workflow_uuid = %s",
            (wid,),
        )
    hour = client.get("/workflows", params={"window": "1h", "id_prefix": prefix}).text
    day = client.get("/workflows", params={"window": "24h", "id_prefix": prefix}).text
    assert wid not in hour
    assert wid in day
    filtered = client.get("/workflows", params={"window": "1h", "process": "test"}).text
    assert 'href="/workflows?status=ERROR&amp;window=1h&amp;process=test"' in filtered


def test_the_list_hides_scheduled_ticks_until_asked(client: TestClient) -> None:
    hidden = client.get("/workflows").text
    assert "show scheduled ticks" in hidden
    shown = client.get("/workflows", params={"scheduled": "1"}).text
    assert "hide scheduled ticks" in shown
    assert "scheduled=1" in shown, "the live refresh keeps the choice"
