import sys
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
import stranded
from fake_mail import PASSWORD, install

from mappd import doctor, gateway, mail, ops, users, workflows
from mappd.channels.email import MailError
from mappd.db import connect, migrate
from mappd.doctor import Check, redact, run_all
from mappd.process import Process, register_process
from mappd.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from fake_gateway import Gateway


AGENTS = """
from pydantic_ai import Agent

from mappd import gateway
from mappd.process import Process

classifier = Agent(gateway.model("{model}"), name="classifier")
process = Process(name="{name}")
"""


@pytest.fixture
def healthy(
    dbos_runtime: Settings, fake_gateway: Gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    monkeypatch.setattr(sys, "path", list(sys.path))
    processes = tmp_path / f"procs_{uuid4().hex}"
    (processes / "support").mkdir(parents=True)
    (processes / "support" / "__init__.py").write_text(AGENTS.format(model="fast", name="support"))
    register_process(processes, "support")
    return replace(
        dbos_runtime,
        gateway_url=fake_gateway.url,
        mlflow_url=fake_gateway.root,
        processes_dir=processes,
    )


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def test_every_layer_ok(healthy: Settings) -> None:
    checks = run_all(healthy)
    assert [(c.layer, c.group, c.name) for c in checks] == [
        ("M", "gateway", "gateway"),
        ("A", "agents", "agents"),
        ("P", "postgres", "postgres"),
        ("P", "processes", "processes"),
        ("P", "processes", "policies"),
        ("P", "processes", "auth"),
        ("P", "processes", "cases"),
        ("·", "traces", "mlflow"),
        ("D", "workflows", "workflows"),
    ]
    assert all(c.ok for c in checks), checks
    agents = by_name(checks)["agents"]
    assert agents.detail == "1 agent in 1 process, every model resolves"
    assert agents.target is None


def test_health_run_skips_workflows(healthy: Settings) -> None:
    # Stranded workflows after a deploy are an operator item, not an app outage.
    assert "workflows" in by_name(run_all(healthy))
    assert "workflows" not in by_name(run_all(healthy, health=True))


def test_cases_line_informs_and_never_fails(
    healthy: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stuck = ops.OpsReport(
        processes=[
            ops.ProcessOps(process="p", waiting=0, oldest_waiting=None, stuck=2, stuck_cases=[])
        ],
        outbox=ops.OutboxOps(unknown=0, failed=0, webhooks_redelivering=0, webhooks_given_up=1),
    )
    monkeypatch.setattr(ops, "report", lambda conn, declared: stuck)
    cases = by_name(run_all(healthy))["cases"]
    assert (cases.status, cases.detail) == ("ok", "2 stuck cases, 1 webhook given up — see /ops")
    assert by_name(run_all(healthy, health=True))["cases"].status == "skip"


def test_cases_line_skips_when_it_cannot_read(healthy: Settings) -> None:
    down = replace(healthy, database_url="postgresql://u@127.0.0.1:9/db")
    cases = by_name(run_all(down))["cases"]
    assert cases.status == "skip"
    assert cases.detail.startswith("not read: ")
    assert doctor.check_cases(healthy, processes_ok=False).status == "skip"


def test_gateway_down_fails_model_and_agents(healthy: Settings) -> None:
    checks = by_name(run_all(replace(healthy, gateway_url="http://127.0.0.1:9/v1")))
    assert checks["gateway"].status == "fail"
    assert checks["gateway"].fix == "docker compose up -d --wait agentgateway"
    assert checks["agents"].status == "fail"


def test_gateway_that_cannot_call_its_provider_fails(
    healthy: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_key(model_name: str, settings: Settings | None = None) -> None:
        raise gateway.GatewayError(f"{model_name}: 401 missing api key")

    monkeypatch.setattr(gateway, "probe", no_key)
    check = by_name(run_all(healthy))["gateway"]
    assert check.status == "fail"
    assert "a call failed: fast: 401" in check.detail
    assert check.fix is not None
    assert "OPENROUTER_API_KEY" in check.fix
    assert by_name(run_all(healthy, health=True))["gateway"].ok, "the healthcheck makes no call"


def test_gateway_check_says_which_model_answered(healthy: Settings) -> None:
    assert by_name(run_all(healthy))["gateway"].detail == "3 models, fast answers"


def test_unknown_agent_model_fails_agents(healthy: Settings) -> None:
    (healthy.processes_dir / "rfq").mkdir()
    (healthy.processes_dir / "rfq" / "__init__.py").write_text(
        AGENTS.format(model="huge", name="rfq")
    )
    register_process(healthy.processes_dir, "rfq")
    agents = by_name(run_all(healthy))["agents"]
    assert not agents.ok
    assert "classifier→huge" in agents.detail


def test_broken_process_fails_processes_and_agents(healthy: Settings) -> None:
    (healthy.processes_dir / "broken").mkdir()
    (healthy.processes_dir / "broken" / "__init__.py").write_text("raise RuntimeError('boom')\n")
    register_process(healthy.processes_dir, "broken")
    checks = by_name(run_all(healthy))
    assert not checks["processes"].ok
    assert "boom" in checks["processes"].detail
    assert not checks["agents"].ok


def test_pending_migrations_fail_postgres(healthy: Settings, fresh_database_url: str) -> None:
    postgres = by_name(run_all(replace(healthy, database_url=fresh_database_url)))["postgres"]
    assert postgres.status == "fail"
    assert postgres.detail == "5 migrations pending"
    assert postgres.fix == "mappd db migrate"


def test_a_database_ahead_is_postgres_ok(healthy: Settings, fresh_database_url: str) -> None:
    # A rollback: the newer code migrated further; the old code must not call that a failure.
    settings = replace(healthy, database_url=fresh_database_url)
    migrate.upgrade(settings)
    with connect(settings) as conn:
        conn.execute("INSERT INTO alembic_version (version_num) VALUES ('feedfacecafe')")
    postgres = by_name(run_all(settings))["postgres"]
    assert postgres.status == "ok"
    assert postgres.detail == "database ahead of this code (rollback): feedfacecafe"


def test_stranded_workflow_fails_workflows(healthy: Settings) -> None:
    wid = stranded.strand(healthy, "old")
    try:
        check = by_name(run_all(healthy))["workflows"]
        assert check.status == "fail"
        assert "1 stranded on version old" in check.detail
        assert check.fix is not None
        assert "mappd workflow recover" in check.fix
        assert "/workflows?status=stranded" in check.fix
    finally:
        workflows.cancel(healthy, wid)


def test_uninitialized_system_database_says_to_start_the_app(
    healthy: Settings, recreate_database: Callable[[str], str]
) -> None:
    fresh = replace(healthy, system_database_url=recreate_database("mappd_test_doctor_dbos"))
    check = by_name(run_all(fresh))["workflows"]
    assert check.status == "fail"
    assert check.detail == "DBOS has not initialized this database"
    assert check.fix == "start the app once (mappd serve)"


def test_traces_down_fails_traces(healthy: Settings) -> None:
    mlflow = by_name(run_all(replace(healthy, mlflow_url="http://127.0.0.1:9")))["mlflow"]
    assert mlflow.status == "fail"
    assert mlflow.fix == "docker compose up -d --wait mlflow"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://user:pw@db.internal:5432/mappd", "postgresql://db.internal:5432/mappd"),
        (
            "postgresql://mappd@127.0.0.1:5432/mappd?password=SECRET",
            "postgresql://127.0.0.1:5432/mappd",
        ),
        ("postgresql://u:SE/CRET@h:5432/db", "postgresql://h:5432/db"),
        ("postgresql://u:pw@h1:5432,h2:5432/db", "postgresql://h1:5432,h2:5432/db"),
        (
            "postgresql://u:pw@[::1]:5432/db?sslmode=require",
            "postgresql://[::1]:5432/db?sslmode=require",
        ),
        ("http://127.0.0.1:4000/v1", "http://127.0.0.1:4000/v1"),
        ("no scheme at all", "<redacted>"),
    ],
)
def test_redact_never_shows_credentials(url: str, expected: str) -> None:
    assert redact(url) == expected


def test_doctor_survives_urls_urlparse_rejects(healthy: Settings) -> None:
    odd = replace(healthy, database_url="postgresql://u:SE/CRET@127.0.0.1:9/db")
    postgres = by_name(run_all(odd))["postgres"]
    assert not postgres.ok
    assert "CRET" not in (postgres.target or "") + postgres.detail


def test_policies_check_names_a_broken_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "autonomy.yaml"
    bad.write_text("shadow: maybe\n")
    monkeypatch.setattr(doctor, "load", lambda _s: {"demo": Process(name="demo", autonomy=bad)})
    check = doctor.check_policies(Settings(processes_dir=tmp_path))
    assert (check.layer, check.group, check.name, check.status) == (
        "P",
        "processes",
        "policies",
        "fail",
    )
    assert "autonomy.yaml" in check.detail


MAILBOX_PROCESS = """
from mappd.process import Mailbox, Process


async def receive(email):
    return None


process = Process(name="{name}", mailboxes=(Mailbox(name="{name}", receive=receive),))
"""


@pytest.fixture
def mailboxes(healthy: Settings, monkeypatch: pytest.MonkeyPatch) -> tuple[Settings, str]:
    """`healthy` plus a process with mailbox `box_<tag>` (fake servers) and `off_<tag>`."""
    tag = uuid4().hex[:6]
    for name in (f"box_{tag}", f"off_{tag}"):
        (healthy.processes_dir / name).mkdir()
        (healthy.processes_dir / name / "__init__.py").write_text(MAILBOX_PROCESS.format(name=name))
        register_process(healthy.processes_dir, name)
    monkeypatch.delenv(f"MAPPD_OFF_{tag.upper()}_MAIL_MODE", raising=False)
    install(monkeypatch, f"box_{tag}")
    return healthy, tag


def test_doctor_checks_each_imap_mailbox(mailboxes: tuple[Settings, str]) -> None:
    settings, tag = mailboxes
    checks = by_name(run_all(settings))
    box = checks[f"mail:box_{tag}"]
    assert (box.layer, box.group, box.status) == ("P", "processes", "ok")
    assert box.detail == "imap ok (INBOX, 0 messages), smtp ok"
    assert box.target == "imap://imap.mappd.test:993"


def test_doctor_reports_off_mailboxes(mailboxes: tuple[Settings, str]) -> None:
    settings, tag = mailboxes
    off = by_name(run_all(settings))[f"mail:off_{tag}"]
    assert (off.status, off.ok) == ("off", True)
    assert off.target is None
    assert f"MAPPD_OFF_{tag.upper()}_MAIL_MODE" in off.detail


def test_doctor_fails_a_mailbox_whose_login_fails(
    mailboxes: tuple[Settings, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, tag = mailboxes

    class Refusing:
        def check(self) -> str:
            raise MailError("IMAP imap.mappd.test:993: error: AUTHENTICATIONFAILED")

    monkeypatch.setattr(mail, "imap_client", lambda _cfg: Refusing())
    box = by_name(run_all(settings))[f"mail:box_{tag}"]
    assert box.status == "fail"
    assert "IMAP" in box.detail
    assert "AUTHENTICATIONFAILED" in box.detail


def test_doctor_mail_check_hides_the_password(
    mailboxes: tuple[Settings, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, tag = mailboxes
    monkeypatch.setenv(f"MAPPD_BOX_{tag.upper()}_IMAP_HOST", "")
    checks = run_all(settings)
    box = by_name(checks)[f"mail:box_{tag}"]
    assert box.status == "fail"
    assert box.target is None
    assert "IMAP_HOST" in box.detail
    assert PASSWORD not in repr(checks)


def test_health_mode_does_not_log_in_to_mail_servers(
    mailboxes: tuple[Settings, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, tag = mailboxes

    def never(_cfg: object) -> object:
        raise AssertionError("the healthcheck must not connect to mail servers")

    monkeypatch.setattr(mail, "imap_client", never)
    monkeypatch.setattr(mail, "smtp_client", never)
    box = by_name(run_all(settings, health=True))[f"mail:box_{tag}"]
    assert (box.status, box.ok) == ("skip", True)
    assert box.detail == "skipped in the healthcheck"


def test_a_stalled_poll_fails_the_mailbox(mailboxes: tuple[Settings, str]) -> None:
    settings, tag = mailboxes
    name = f"box_{tag}"
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO mappd_mail_cursors (mailbox, uidvalidity, last_uid, polled_at)"
            " VALUES (%s, 1, 5, now() - interval '2 hours')",
            (name,),
        )
    box = by_name(run_all(settings))[f"mail:{name}"]
    assert box.status == "fail"
    assert "last poll" in box.detail
    assert "see its log" not in box.detail
    assert box.fix == "check the app log"


def test_auth_check_none_is_off() -> None:
    check = doctor.check_auth(Settings(auth="none"))
    assert (check.name, check.status, check.ok) == ("auth", "off", True)
    assert "MAPPD_AUTH=none" in check.detail


def test_auth_check_jwt_needs_a_secret(healthy: Settings) -> None:
    for secret in ("", "too-short"):
        check = doctor.check_auth(replace(healthy, auth="jwt", auth_secret=secret))
        assert check.status == "fail"
        assert "MAPPD_AUTH_SECRET" in check.detail


def test_auth_check_jwt_needs_an_enabled_admin(healthy: Settings) -> None:
    settings = replace(healthy, auth="jwt", auth_secret="s" * 40)
    with connect(settings) as conn:
        conn.execute("DELETE FROM mappd_users")
    check = doctor.check_auth(settings)
    assert check.status == "fail"
    assert check.fix == "mappd user add <name> --role admin --password-stdin"
    with connect(settings) as conn:
        users.add(conn, "doc-olga", "secret-123", ("operator",))
    assert doctor.check_auth(settings).status == "fail", "an operator is not an admin"
    with connect(settings) as conn:
        users.set_roles(conn, "doc-olga", ("admin",))
    check = doctor.check_auth(settings)
    assert check.status == "ok"
    assert "jwt" in check.detail
    assert "1 user" in check.detail
    with connect(settings) as conn:
        users.set_disabled(conn, "doc-olga", True)
    assert doctor.check_auth(settings).status == "fail", "a disabled admin does not count"


def test_auth_check_header_is_ok_without_a_users_table() -> None:
    check = doctor.check_auth(Settings(auth="header"))
    assert (check.status, check.ok) == ("ok", True)
    assert "header" in check.detail
