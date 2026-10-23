"""Properties of the stack definition that the acceptance scenarios cannot see."""

import re
from pathlib import Path
from typing import Any

import yaml

from mappd.settings import Settings

REPO = Path(__file__).parents[1]

# compose.deploy.yaml uses `!reset` (Compose >= 2.24); map it to a sentinel for assertions.
RESET = "<!reset>"


class ComposeLoader(yaml.SafeLoader):
    pass


ComposeLoader.add_constructor("!reset", lambda loader, node: RESET)

STACK: dict[str, Any] = yaml.safe_load((REPO / "compose.yaml").read_text())
APP: dict[str, Any] = yaml.safe_load((REPO / "compose.app.yaml").read_text())
DEPLOY: dict[str, Any] = yaml.load(
    (REPO / "compose.deploy.yaml").read_text(),
    Loader=ComposeLoader,  # noqa: S506 - ComposeLoader is SafeLoader plus !reset
)
STACK_SERVICES: dict[str, Any] = STACK["services"]
APP_SERVICES: dict[str, Any] = APP["services"]
DEPLOY_SERVICES: dict[str, Any] = DEPLOY["services"]
ENV_EXAMPLE = (REPO / ".env.example").read_text()


def test_the_stack_file_is_infrastructure_only() -> None:
    # compose.yaml is the stack; the application lives in compose.app.yaml.
    assert set(STACK_SERVICES) == {"postgres", "mlflow", "agentgateway", "greenmail"}
    assert set(APP_SERVICES) == {
        "migrate",
        "app",
        "mock-llm",
        "mock-webhook",
        "mock-telegram",
        "mock-mcp",
        "slow-smtp",
    }


def test_long_running_services_come_back_after_a_crash_or_reboot() -> None:
    for name in ("postgres", "mlflow", "agentgateway", "greenmail"):
        assert STACK_SERVICES[name].get("restart") == "unless-stopped", name
    assert APP_SERVICES["app"].get("restart") == "unless-stopped"


def test_no_service_builds_the_app_context_in_the_stack_file() -> None:
    for name, service in STACK_SERVICES.items():
        build = service.get("build")
        context = build.get("context") if isinstance(build, dict) else build
        assert context != ".", f"{name} builds the app image inside the stack file"


def test_the_mail_server_runs_without_a_profile_on_localhost() -> None:
    # Approvals send email: the dev stack's GreenMail is there without the mocks profile.
    greenmail = STACK_SERVICES["greenmail"]
    assert greenmail["image"] == "${MAPPD_GREENMAIL_IMAGE:-greenmail/standalone:2.1.14}"
    assert "profiles" not in greenmail
    assert all(str(p).startswith("127.0.0.1:") for p in greenmail["ports"])
    assert APP_SERVICES["mock-llm"]["profiles"] == ["mocks"]
    assert APP_SERVICES["slow-smtp"]["profiles"] == ["mocks"]


def test_the_app_reads_both_mailboxes_and_mail_is_off_by_default() -> None:
    env = APP_SERVICES["app"]["environment"]
    for box in ("SUPPORT", "RFQ"):
        assert env[f"MAPPD_{box}_MAIL_MODE"] == f"${{MAPPD_{box}_MAIL_MODE:-off}}"
        for key in ("MAIL_FROM", "IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "SMTP_HOST"):
            assert f"MAPPD_{box}_{key}" in env, key
    assert "MAPPD_SUPPORT_MAILER_URL" not in env
    assert "MAILER" not in ENV_EXAMPLE
    assert "MAPPD_SUPPORT_MAIL_MODE=imap\n" in ENV_EXAMPLE


def test_webhooks_are_off_unless_configured() -> None:
    # The help desk stand-in is a mock; a real deploy names its own receiver in .env.
    assert APP_SERVICES["mock-webhook"]["profiles"] == ["mocks"]
    env = APP_SERVICES["app"]["environment"]
    for proc in ("SUPPORT", "RFQ"):
        assert env[f"MAPPD_{proc}_WEBHOOK_URL"] == f"${{MAPPD_{proc}_WEBHOOK_URL:-}}"
        secret = f"${{MAPPD_{proc}_WEBHOOK_SECRET:-}}"
        assert env[f"MAPPD_{proc}_WEBHOOK_SECRET"] == secret
        assert f"MAPPD_{proc}_WEBHOOK_URL=\n" in ENV_EXAMPLE
    assert "MAPPD_SUPPORT_TELEGRAM_SECRET" not in env, "support takes no Telegram tickets"


def test_the_autonomy_policy_defaults_to_the_reviewed_file() -> None:
    env = APP_SERVICES["app"]["environment"]
    assert env["MAPPD_SUPPORT_AUTONOMY"] == "${MAPPD_SUPPORT_AUTONOMY:-}"
    assert env["MAPPD_RFQ_AUTONOMY"] == "${MAPPD_RFQ_AUTONOMY:-}"
    assert env["MAPPD_WORKFLOWS_PAYLOADS"] == "${MAPPD_WORKFLOWS_PAYLOADS:-}"


def test_the_app_version_is_not_a_constant() -> None:
    # A constant would let a rebuilt image replay old journals against new code.
    version = APP_SERVICES["app"]["environment"]["MAPPD_APP_VERSION"]
    assert version == "${MAPPD_APP_VERSION:-}"


def test_the_shutdown_budget_fits_the_grace_period() -> None:
    cmd = (REPO / "Dockerfile").read_text()
    match = re.search(r'"--timeout-graceful-shutdown", "(\d+)"', cmd)
    assert match is not None
    grace = int(APP_SERVICES["app"]["stop_grace_period"].removesuffix("s"))
    assert int(match.group(1)) + Settings().drain_seconds < grace


def test_no_credentials_are_hardcoded() -> None:
    # Credentials come from .env; a hardcoded mappd:mappd would ignore them.
    for file in ("compose.yaml", "compose.app.yaml"):
        text = (REPO / file).read_text()
        assert "mappd:mappd" not in text, f"{file} hardcodes database credentials"


def test_every_referenced_variable_is_documented_in_env_example() -> None:
    referenced = set()
    for file in ("compose.yaml", "compose.app.yaml", "compose.deploy.yaml"):
        referenced |= set(re.findall(r"\$\{([A-Z_]+)", (REPO / file).read_text()))
    undocumented = {v for v in referenced if not re.search(rf"^#?\s*{v}=", ENV_EXAMPLE, re.M)}
    assert not undocumented, f"not in .env.example: {sorted(undocumented)}"


def test_the_demo_env_sets_only_documented_variables() -> None:
    demo = (REPO / ".env.demo").read_text()
    keys = re.findall(r"^([A-Z_]+)=", demo, re.M)
    assert len(keys) == len(set(keys)), "duplicate keys in .env.demo"
    undocumented = {k for k in keys if not re.search(rf"^#?\s*{k}=", ENV_EXAMPLE, re.M)}
    assert not undocumented, f"not in .env.example: {sorted(undocumented)}"
    assert "MAPPD_GATEWAY_CONFIG=gateway.mocks.yaml" in demo
    assert "OPENROUTER_API_KEY" not in keys, "the demo needs no provider key"


def test_the_deploy_overlay_pulls_images_instead_of_building() -> None:
    # `!reset null` deletes the build inherited from compose.app.yaml; a server only pulls.
    built = [name for name, service in APP_SERVICES.items() if "build" in service]
    assert set(built) == set(APP_SERVICES), "expected every app service to build locally"
    for name in built:
        service = DEPLOY_SERVICES[name]
        assert service["build"] == RESET, name
        assert service["image"].startswith("${MAPPD_APP_IMAGE:?"), name
    mlflow = DEPLOY_SERVICES["mlflow"]
    assert mlflow["build"] == RESET
    assert mlflow["image"].startswith("${MAPPD_MLFLOW_IMAGE:?")


def test_greenmail_is_behind_the_dev_profile_on_servers_only() -> None:
    assert "profiles" not in STACK_SERVICES["greenmail"]
    assert DEPLOY_SERVICES["greenmail"]["profiles"] == ["dev"]


def test_the_overlay_takes_mail_servers_from_the_environment() -> None:
    # compose.app.yaml points mail at the dev GreenMail; a server's .env names real servers.
    for name in ("app", "migrate"):
        env = DEPLOY_SERVICES[name]["environment"]
        for box in ("SUPPORT", "RFQ"):
            for proto in ("IMAP", "SMTP"):
                for key in ("HOST", "PORT", "TLS"):
                    var = f"MAPPD_{box}_{proto}_{key}"
                    assert env[var] == f"${{{var}:-}}", var


def test_the_deploy_release_paths_exist() -> None:
    for path in (
        "compose.yaml",
        "compose.app.yaml",
        "compose.deploy.yaml",
        "deploy/agentgateway",
        "deploy/postgres",
        "deploy/mlflow",
    ):
        assert (REPO / path).exists(), path


def test_the_healthcheck_does_not_log_in_to_mail_servers() -> None:
    assert APP_SERVICES["app"]["healthcheck"]["test"] == ["CMD", "mappd", "doctor", "--health"]


def test_every_telegram_setting_reaches_the_app_and_is_documented() -> None:
    # MAPPD_INSTALLATION_BOT_NAME was read but neither documented nor passed to the container,
    # so a deploy could not set it and every deep link named another bot.
    code = (REPO / "processes/installation/telegram.py").read_text()
    read = set(re.findall(r'"(MAPPD_INSTALLATION_[A-Z_]+)"', code))
    app_env = yaml.safe_load((REPO / "compose.app.yaml").read_text())["x-app"]["environment"]
    assert "MAPPD_INSTALLATION_BOT_NAME" in read
    assert not read - set(app_env), f"not passed to the app: {sorted(read - set(app_env))}"
    undocumented = {v for v in read if not re.search(rf"^#?\s*{v}=", ENV_EXAMPLE, re.M)}
    assert not undocumented, f"not in .env.example: {sorted(undocumented)}"
