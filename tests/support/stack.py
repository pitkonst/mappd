"""Drive the compose stack for the acceptance tests.

A separate compose project on its own host ports, so a developer's stack keeps running. The app
is killed the way production kills it: SIGKILL for a crash, SIGTERM (compose stop) for a deploy.
"""

import json
import os
import subprocess
import tempfile
import time
from email.utils import make_msgid
from pathlib import Path
from typing import Any

import httpx2
import psycopg
from dbos import DBOSClient

from mappd.channels.email import (
    Email,
    ImapClient,
    MailConfig,
    MailError,
    SmtpClient,
    build,
    parse,
)

REPO = Path(__file__).parents[2]
PROJECT = "mappd-acceptance"
PORTS = {
    "MAPPD_PG_PORT": "15432",
    "MAPPD_APP_PORT": "18000",
    "MAPPD_GATEWAY_PORT": "14000",
    "MAPPD_MLFLOW_PORT": "15001",
    "MAPPD_MOCK_LLM_PORT": "19000",
    "MAPPD_GREENMAIL_SMTP_PORT": "13025",
    "MAPPD_GREENMAIL_IMAP_PORT": "13143",
    "MAPPD_GREENMAIL_API_PORT": "18080",
    "MAPPD_MOCK_WEBHOOK_PORT": "19200",
    "MAPPD_MOCK_TELEGRAM_PORT": "19300",
    "MAPPD_MOCK_MCP_PORT": "19400",
    "MAPPD_GATEWAY_MCP_PORT": "13000",
}
APP = "http://127.0.0.1:18000"
LLM = "http://127.0.0.1:19000"
GREENMAIL = "http://127.0.0.1:18080"
SMTP_PORT, IMAP_PORT = 13025, 13143
HOOK = "http://127.0.0.1:19200"
TELEGRAM = "http://127.0.0.1:19300"
MCP = "http://127.0.0.1:19400"
# The app reads its autonomy policy from here (compose.policy.yaml mounts it at /policy).
POLICY_DIR = Path(tempfile.mkdtemp(prefix="mappd-policy-"))
REVIEW_POLICY = "shadow: false\ndefault: review\n"
(POLICY_DIR / "autonomy.yaml").write_text(REVIEW_POLICY)
PG = "postgresql://mappd:mappd@127.0.0.1:15432"
# Checked against DBOS 3.2.0's system schema; versions, queues and schedules stay.
DBOS_TABLES = (
    "dbos.workflow_status, dbos.operation_outputs, dbos.workflow_input, dbos.workflow_output,"
    " dbos.notifications, dbos.workflow_events, dbos.workflow_events_history, dbos.streams"
)


def compose(
    *args: str, version: str = "v1", check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run `docker compose` on the acceptance project with the mocks."""
    env = {
        **os.environ,
        **PORTS,
        "COMPOSE_PROFILES": "mocks",
        "MAPPD_GATEWAY_CONFIG": "gateway.mocks.yaml",
        "MAPPD_APP_VERSION": version,
        "MAPPD_SUPPORT_WEBHOOK_URL": "http://mock-webhook:9200/hook",
        "MAPPD_SUPPORT_WEBHOOK_SECRET": "acceptance-webhook-secret",
        "MAPPD_TEST_POLICY_DIR": str(POLICY_DIR),
        "MAPPD_INSTALLATION_TELEGRAM_MODE": "polling",
        "MAPPD_INSTALLATION_TELEGRAM_TOKEN": "test-token",
        "MAPPD_INSTALLATION_TELEGRAM_API_URL": "http://mock-telegram:9300",
        # Both mailboxes on GreenMail, polled every second; support sends through slow-smtp
        # (tests/acceptance/compose.policy.yaml). Backfill: each scenario starts from a purge.
        "MAPPD_MAIL_POLL_SECONDS": "1",
        "MAPPD_SUPPORT_MAIL_MODE": "imap",
        "MAPPD_SUPPORT_IMAP_BACKFILL": "true",
        "MAPPD_RFQ_MAIL_MODE": "imap",
        "MAPPD_RFQ_IMAP_BACKFILL": "true",
    }
    result = subprocess.run(  # noqa: S603 - fixed argv
        [  # noqa: S607 - fixed argv
            "docker",
            "compose",
            "-p",
            PROJECT,
            "-f",
            str(REPO / "compose.yaml"),
            "-f",
            str(REPO / "compose.app.yaml"),
            "-f",
            str(REPO / "tests/acceptance/compose.policy.yaml"),
            *args,
        ],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{result.stderr[-3000:]}")
    return result


def wait_healthy(timeout: float = 90) -> None:
    """Until the app answers /health."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx2.get(f"{APP}/health", timeout=2).status_code == 200:
                return
        except httpx2.HTTPError:
            pass
        time.sleep(0.5)
    raise AssertionError(
        f"app not healthy:\n{compose('logs', '--tail=50', 'app', check=False).stdout}"
    )


def up() -> None:
    """Build and start everything, migrate, start the app at v1, seed the knowledge base."""
    compose(
        "up",
        "-d",
        "--build",
        "--wait",
        "postgres",
        "mlflow",
        "mock-llm",
        "greenmail",
        "slow-smtp",
        "mock-webhook",
        "mock-telegram",
        "mock-mcp",
        "agentgateway",
    )
    compose("run", "--rm", "migrate")
    start("v1")
    compose("exec", "-T", "app", "python", "-m", "processes.support.data.seed")


def down() -> None:
    """Remove the project with its volumes."""
    compose("down", "-v", check=False)


def start(version: str) -> None:
    """Start the app at `version` (recreated if the version changed) and wait for it."""
    compose("up", "-d", "--no-deps", "app", version=version)
    wait_healthy()


def restart() -> None:
    """Restart the app (it reads its policy at startup) and wait for it."""
    compose("restart", "app")
    wait_healthy()


def set_policy(text: str) -> None:
    """Write the app's autonomy policy and restart it to load the file."""
    (POLICY_DIR / "autonomy.yaml").write_text(text)
    restart()


def kill() -> None:
    """A crash: SIGKILL, no drain."""
    compose("kill", "-s", "SIGKILL", "app")


def stop_graceful() -> float:
    """A deploy: SIGTERM and wait; returns how long the drain took."""
    started = time.monotonic()
    compose("stop", "-t", "60", "app")
    return time.monotonic() - started


def reset() -> None:
    """A clean slate between scenarios: app stopped, tables and mock counters emptied."""
    compose("kill", "-s", "SIGKILL", "app", check=False)
    with psycopg.connect(f"{PG}/mappd", autocommit=True) as conn:
        conn.execute(
            "TRUNCATE support_tickets, rfq_requests, installation_deadlines, installation_messages,"
            " installation_jobs, installation_installers, installation_polling, actions,"
            " mappd_mail_cursors RESTART IDENTITY CASCADE"
        )
    with psycopg.connect(f"{PG}/mappd_dbos", autocommit=True) as conn:
        conn.execute(f"TRUNCATE {DBOS_TABLES} CASCADE")
    latest_version("v1")
    httpx2.post(f"{LLM}/calls/reset", timeout=5)
    httpx2.post(f"{LLM}/delay/draft/0", timeout=5)
    purge_mail()
    httpx2.post(f"{HOOK}/reset", timeout=5)
    httpx2.post(f"{TELEGRAM}/test/reset", timeout=5)
    httpx2.post(f"{MCP}/test/reset", timeout=5)
    (POLICY_DIR / "autonomy.yaml").write_text(REVIEW_POLICY)  # loaded when start() runs the app


def latest_version(version: str) -> None:
    """Make `version` DBOS's latest again: its scheduler queues scheduled workflows (mail and
    Telegram polls) for the latest version, so after a v2 scenario a v1 app would never run them.
    """
    client = DBOSClient(system_database_url=f"{PG}/mappd_dbos", application_name="mappd")
    try:
        if version in {v["version_name"] for v in client.list_application_versions()}:
            client.set_latest_application_version(version)
    finally:
        client.destroy()


def doctor() -> dict[str, Any]:
    """`mappd doctor --json` inside the app container."""
    result = compose("exec", "-T", "app", "mappd", "doctor", "--json", check=False)
    report: dict[str, Any] = json.loads(result.stdout)
    return report


def started_at() -> str:
    """When the app container last started (changes when Docker restarts it)."""
    container = compose("ps", "-q", "app").stdout.strip()
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["docker", "inspect", "-f", "{{.State.StartedAt}}", container],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


MAIL_PASSWORD = "mappd"  # noqa: S105 - GreenMail's synthetic dev users (compose.yaml)
DEV_USERS = frozenset({"support@mappd.test", "rfq@mappd.test", "customer@mappd.test"})


def _mail_config(address: str, password: str) -> MailConfig:
    name = "probe"
    return MailConfig.from_env(
        name,
        {
            "MAPPD_PROBE_MAIL_MODE": "imap",
            "MAPPD_PROBE_MAIL_FROM": address,
            "MAPPD_PROBE_IMAP_HOST": "127.0.0.1",
            "MAPPD_PROBE_IMAP_PORT": str(IMAP_PORT),
            "MAPPD_PROBE_IMAP_TLS": "none",
            "MAPPD_PROBE_IMAP_USER": address,
            "MAPPD_PROBE_IMAP_PASSWORD": password,
            "MAPPD_PROBE_SMTP_HOST": "127.0.0.1",
            "MAPPD_PROBE_SMTP_PORT": str(SMTP_PORT),
            "MAPPD_PROBE_SMTP_TLS": "none",
            "MAPPD_PROBE_SMTP_USER": address,
            "MAPPD_PROBE_SMTP_PASSWORD": password,
        },
    )


def inbox(address: str) -> list[Email]:
    """Every message in `address`'s GreenMail inbox, oldest first (read-only).

    GreenMail creates a recipient it does not know on delivery, with the address as password.
    """
    cfg = _mail_config(address, MAIL_PASSWORD if address in DEV_USERS else address)
    try:
        fetched = ImapClient(cfg).fetch_since(None, 0, limit=500)
    except MailError:
        if address in DEV_USERS:
            raise
        return []  # nothing delivered yet: GreenMail has not created the user
    return [parse(m.raw) for m in fetched.messages if m.raw is not None]


def send_mail(
    to: str,
    *,
    sender: str = "customer@mappd.test",
    subject: str,
    body: str,
    in_reply_to: str | None = None,
) -> str:
    """Send one email into GreenMail from `sender` (logged in as customer@); its Message-ID."""
    message = build(
        sender=sender,
        to=to,
        subject=subject,
        text=body,
        message_id=make_msgid(domain="mappd.test"),
        reply_to=None,
    )
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    SmtpClient(_mail_config("customer@mappd.test", MAIL_PASSWORD)).send(message)
    return str(message["Message-ID"])


def purge_mail() -> None:
    """Empty every GreenMail mailbox."""
    httpx2.post(f"{GREENMAIL}/api/mail/purge", timeout=10).raise_for_status()
