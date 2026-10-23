"""Health of every MAPP-D layer, grouped checks.

Order: gateway (M), agents (A), postgres (P), processes/policies/mailboxes (P), traces (·),
workflows (D). A failing check names the command that fixes it. Error texts are
credential-free: URLs are redacted and database errors are reported by type and first line.
"""

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, urlencode

import httpx2
import psycopg.errors
import sqlalchemy.exc
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel

from mappd import db, gateway, mail, ops, users, workflows
from mappd.app import discover, load
from mappd.autonomy import PolicyError
from mappd.autonomy import load as load_policy
from mappd.channels.email import MailConfig, MailConfigError, MailError
from mappd.db import migrate
from mappd.web import auth

if TYPE_CHECKING:
    from mappd.settings import Settings

TIMEOUT = 3.0
SAFE_QUERY_KEYS = frozenset(
    {"sslmode", "connect_timeout", "application_name", "target_session_attrs"}
)
HEALTHY = 200

Status = Literal["ok", "off", "skip", "fail"]

# Display order and header of each group of checks.
GROUPS: dict[str, tuple[str, str]] = {
    "gateway": ("M", "model gateway"),
    "agents": ("A", "agents"),
    "postgres": ("P", "Postgres"),
    "processes": ("P", "processes"),
    "traces": ("·", "traces"),
    "workflows": ("D", "durable execution"),
}


@dataclass(frozen=True)
class Check:
    """One check of `mappd doctor`."""

    layer: str
    group: str
    name: str
    status: Status
    target: str | None  # a real location (URL/DSN/dir) or None
    detail: str
    fix: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the check passes; off and skip are not failures."""
        return self.status != "fail"


def redact(url: str) -> str:
    """Drop credentials so doctor output is safe to paste into an issue; never raises.

    Works on the raw string rather than urlparse, which rejects multi-host URLs and passwords
    with "/". Only harmless query keys survive (libpq also accepts ?password=...).
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return "<redacted>"
    location, _, query = rest.partition("?")
    location = location.rpartition("@")[2]
    kept = [(k, v) for k, v in parse_qsl(query) if k in SAFE_QUERY_KEYS]
    return f"{scheme}://{location}" + (f"?{urlencode(kept)}" if kept else "")


def _error(e: BaseException) -> str:
    first = str(e).strip().splitlines()[0] if str(e).strip() else ""
    return f"{type(e).__name__}: {first}" if first else type(e).__name__


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else plural or word + 's'}"


PROBE_MODEL = "fast"
NO_KEY_FIX = (
    "set OPENROUTER_API_KEY in .env (or MAPPD_GATEWAY_CONFIG=gateway.mocks.yaml for the mocks),"
    " then docker compose up -d agentgateway"
)


def check_model(settings: Settings, *, probe: bool = True) -> tuple[Check, list[str] | None]:
    """M: the gateway answers, routes at least one model, and one call goes through.

    `probe` makes one 1-token call to the `fast` model (or the first model, if no `fast`): a
    gateway with no provider key lists its models all the same. The healthcheck skips it.
    """
    target = redact(settings.gateway_url)
    try:
        models = gateway.list_models(settings)
    except gateway.GatewayError as e:
        return Check(
            "M",
            "gateway",
            "gateway",
            "fail",
            target,
            f"unreachable: {_error(e.__cause__ or e)}",
            "docker compose up -d --wait agentgateway",
        ), None
    if not models:
        return Check(
            "M",
            "gateway",
            "gateway",
            "fail",
            target,
            "no models configured",
            "add models in deploy/agentgateway/",
        ), models
    detail = _plural(len(models), "model")
    if probe:
        chat = PROBE_MODEL if PROBE_MODEL in models else models[0]
        try:
            gateway.probe(chat, settings)
        except gateway.GatewayError as e:
            failed = f"{detail}, but a call failed: {e}"
            return Check("M", "gateway", "gateway", "fail", target, failed, NO_KEY_FIX), models
        detail += f", {chat} answers"
    return Check("M", "gateway", "gateway", "ok", target, detail), models


def check_processes(settings: Settings) -> Check:
    """P: every registered process loads."""
    target = str(settings.processes_dir)
    try:
        loaded = load(settings)
    except Exception as e:  # any import-time failure of process code is the finding
        return Check("P", "processes", "processes", "fail", target, f"import failed: {_error(e)}")
    names = sorted(loaded)
    return Check("P", "processes", "processes", "ok", target, ", ".join(names) or "none yet")


def _agents(settings: Settings) -> list[Agent[Any, Any]]:
    load(settings)
    package = settings.processes_dir.resolve().name
    found: dict[int, Agent[Any, Any]] = {}
    for module_name, module in list(sys.modules.items()):
        if module_name == package or module_name.startswith(f"{package}."):
            for value in vars(module).values():
                if isinstance(value, Agent):
                    found[id(value)] = value
    return list(found.values())


def check_agents(settings: Settings, models: list[str] | None, processes_ok: bool) -> Check:
    """A: every agent's model is a gateway model the gateway routes."""
    if not processes_ok:
        return Check("A", "agents", "agents", "fail", None, "processes did not load")
    agents = _agents(settings)
    processes = _plural(len(discover(settings.processes_dir)), "process", "processes")
    count = f"{_plural(len(agents), 'agent')} in {processes}"
    if not agents:
        return Check("A", "agents", "agents", "ok", None, "no agents yet")
    if models is None:
        return Check(
            "A", "agents", "agents", "fail", None, f"{count}; gateway unreachable, models unchecked"
        )
    unresolved = []
    for agent in agents:
        model = agent.model
        name = model.model_name if isinstance(model, OpenAIChatModel) else None
        if name is None or name not in models:
            unresolved.append(f"{agent.name}→{name or 'not a gateway model'}")
    if unresolved:
        return Check(
            "A", "agents", "agents", "fail", None, f"{count}; unresolved: {', '.join(unresolved)}"
        )
    return Check("A", "agents", "agents", "ok", None, f"{count}, every model resolves")


def check_postgres(settings: Settings) -> Check:
    """P: the app database answers and its migrations are current."""
    target = redact(settings.database_url)
    try:
        ahead = migrate.ahead(settings)
        todo = migrate.pending(settings)
    except Exception as e:  # connection, auth or migration-script errors
        return Check(
            "P",
            "postgres",
            "postgres",
            "fail",
            target,
            _error(e),
            "docker compose up -d --wait postgres",
        )
    if ahead:
        return Check(
            "P",
            "postgres",
            "postgres",
            "ok",
            target,
            f"database ahead of this code (rollback): {', '.join(ahead)}",
        )
    if todo:
        return Check(
            "P",
            "postgres",
            "postgres",
            "fail",
            target,
            f"{_plural(len(todo), 'migration')} pending",
            "mappd db migrate",
        )
    return Check("P", "postgres", "postgres", "ok", target, "migrations current")


def check_workflows(settings: Settings) -> Check:
    """D: no workflow is stranded on a version that no longer runs."""
    target = redact(settings.system_database_url)
    try:
        stuck = workflows.list_workflows(settings, stranded=True)
    except sqlalchemy.exc.ProgrammingError as e:
        if isinstance(e.orig, psycopg.errors.UndefinedTable):
            return Check(
                "D",
                "workflows",
                "workflows",
                "fail",
                target,
                "DBOS has not initialized this database",
                "start the app once (mappd serve)",
            )
        return Check("D", "workflows", "workflows", "fail", target, _error(e))
    except Exception as e:  # connection and authentication errors
        return Check("D", "workflows", "workflows", "fail", target, _error(e))
    if stuck:
        versions = ", ".join(sorted({w.app_version or "?" for w in stuck}))
        return Check(
            "D",
            "workflows",
            "workflows",
            "fail",
            target,
            f"{len(stuck)} stranded on version {versions}",
            "mappd workflow recover (or /workflows?status=stranded)",
        )
    return Check("D", "workflows", "workflows", "ok", target, "none stranded")


def check_traces(settings: Settings) -> Check:
    """·: MLflow answers its health check."""
    target = redact(settings.mlflow_url)
    try:
        response = httpx2.get(f"{settings.mlflow_url}/health", timeout=TIMEOUT)
    except httpx2.HTTPError as e:
        return Check(
            "·",
            "traces",
            "mlflow",
            "fail",
            target,
            f"unreachable: {_error(e)}",
            "docker compose up -d --wait mlflow",
        )
    status: Status = "ok" if response.status_code == HEALTHY else "fail"
    return Check("·", "traces", "mlflow", status, target, f"HTTP {response.status_code}")


def check_policies(settings: Settings) -> Check:
    """Each process's autonomy.yaml loads (a broken one would stop the app at startup)."""
    target = str(settings.processes_dir)
    try:
        procs = load(settings)
    except Exception as e:
        return Check("P", "processes", "policies", "fail", target, _error(e))
    broken = []
    for name, proc in sorted(procs.items()):
        if proc.autonomy is None:
            continue
        try:
            load_policy(proc.autonomy)
        except PolicyError as e:
            broken.append(f"{name}: {e}")
    if broken:
        return Check("P", "processes", "policies", "fail", target, "; ".join(broken))
    return Check("P", "processes", "policies", "ok", target, "autonomy files load")


STALE_SECONDS = 120  # at least; ten poll intervals otherwise


def _stale(name: str, settings: Settings) -> str | None:
    """How long ago the mailbox was last polled, when that is too long; None otherwise."""
    try:
        cursor = mail.cursor(name, settings)
    except Exception:
        return None
    if cursor is None or cursor.polled_at is None:
        return None
    age = (datetime.now(UTC) - cursor.polled_at).total_seconds()
    if age <= max(STALE_SECONDS, 10 * mail.poll_seconds()):
        return None
    return f"last poll {int(age // 60)} min ago: the app is not polling it"


def check_mailbox(name: str, settings: Settings, *, health: bool = False) -> Check:
    """P: one mailbox logs in over IMAP and SMTP and is being polled; `off` passes.

    The healthcheck (`health`) connects to nothing: a mail outage must not mark the app down.
    """
    check_name = f"mail:{name}"
    try:
        cfg = MailConfig.from_env(name)
    except MailConfigError as e:
        return Check("P", "processes", check_name, "fail", None, str(e))
    if cfg.mode == "off":
        return Check(
            "P",
            "processes",
            check_name,
            "off",
            None,
            f"polling off (MAPPD_{name.upper()}_MAIL_MODE=imap)",
        )
    target = f"imap://{cfg.imap_host}:{cfg.imap_port}"
    if health:
        return Check("P", "processes", check_name, "skip", target, "skipped in the healthcheck")
    ok, detail = _logins(cfg)
    fix = None
    if ok and (stale := _stale(name, settings)):
        ok, detail, fix = False, f"imap ok, smtp ok; {stale}", "check the app log"
    return Check("P", "processes", check_name, "ok" if ok else "fail", target, detail, fix)


def _logins(cfg: MailConfig) -> tuple[bool, str]:
    try:
        folder = mail.imap_client(cfg).check()
    except MailError as e:
        return False, f"IMAP failed: {e}"
    try:
        mail.smtp_client(cfg).check()
    except MailError as e:
        return False, f"imap ok, SMTP failed: {e}"
    return True, f"imap ok ({folder}), smtp ok"


def check_auth(settings: Settings) -> Check:
    """P: sign-in is on and, in jwt mode, an enabled admin exists."""
    if settings.auth == "none":
        return Check(
            "P",
            "processes",
            "auth",
            "off",
            None,
            "MAPPD_AUTH=none: anyone who reaches the app can act",
            "set MAPPD_AUTH for shared deployments",
        )
    if settings.auth == "jwt":
        if len(settings.auth_secret) < auth.MIN_SECRET_LENGTH:
            return Check(
                "P",
                "processes",
                "auth",
                "fail",
                None,
                "MAPPD_AUTH=jwt needs MAPPD_AUTH_SECRET (at least 32 characters)",
                "set MAPPD_AUTH_SECRET (`openssl rand -hex 32`)",
            )
        try:
            with db.connect(settings) as conn:
                all_users = users.list_users(conn)
                admins = users.count_admins(conn)
        except Exception as e:  # the database may be down or unmigrated
            return Check("P", "processes", "auth", "fail", None, _error(e), "mappd db migrate")
        if admins == 0:
            return Check(
                "P",
                "processes",
                "auth",
                "fail",
                None,
                f"jwt: {_plural(len(all_users), 'user')}, none an enabled admin",
                "mappd user add <name> --role admin --password-stdin",
            )
        return Check(
            "P",
            "processes",
            "auth",
            "ok",
            None,
            f"jwt: {_plural(len(all_users), 'user')}, {_plural(admins, 'enabled admin')}",
        )
    mode = "header" if settings.auth == "header" else f"custom ({settings.auth})"
    return Check("P", "processes", "auth", "ok", None, f"{mode}: identity comes from outside")


def check_mail(settings: Settings, processes_ok: bool, *, health: bool = False) -> list[Check]:
    """P: every declared mailbox (none when the processes did not load)."""
    if not processes_ok:
        return []
    names = [m.name for p in load(settings).values() for m in p.mailboxes]
    return [check_mailbox(name, settings, health=health) for name in names]


def check_cases(settings: Settings, processes_ok: bool, *, health: bool = False) -> Check:
    """P: stuck cases and outbox trouble, for information only.

    Always `ok`: a stuck case is an operational finding, not a broken stack, and a failing
    check would fail a deploy and the healthcheck. The healthcheck skips it.
    """
    if health:
        return Check("P", "processes", "cases", "skip", None, "skipped in the healthcheck")
    if not processes_ok:
        return Check("P", "processes", "cases", "skip", None, "processes did not load")
    declared = [(p.name, p.cases) for p in load(settings).values() if p.cases is not None]
    try:
        with db.connect(settings) as conn:
            report = ops.report(conn, declared)
    except Exception as e:  # the postgres check reports the cause
        return Check("P", "processes", "cases", "skip", None, f"not read: {_error(e)}")
    return Check("P", "processes", "cases", "ok", None, ops.summary(report))


def run_all(settings: Settings, *, health: bool = False) -> list[Check]:
    """Every check, in display order; `health` runs the container healthcheck.

    The healthcheck skips mail logins and the workflows check: stranded workflows are an
    operator item, not an app outage — flagging them would fail a deploy right after a
    version change.
    """
    model, models = check_model(settings, probe=not health)
    processes = check_processes(settings)
    return [
        model,
        check_agents(settings, models, processes.ok),
        check_postgres(settings),
        processes,
        check_policies(settings),
        check_auth(settings),
        *check_mail(settings, processes.ok, health=health),
        check_cases(settings, processes.ok, health=health),
        check_traces(settings),
        *([] if health else [check_workflows(settings)]),
    ]
