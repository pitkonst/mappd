"""Settings from MAPPD_* environment variables, with defaults for the local stack.

The compose file sets these for every container; locally the defaults match
`docker compose up`. An empty variable counts as unset. When reading the process
environment, variables from `.env` in the working directory (or MAPPD_ENV_FILE)
apply underneath it — the same file feeds compose, so stack parameters set once
reach both. The database URL defaults derive from MAPPD_PG_USER / MAPPD_PG_PASSWORD
/ MAPPD_PG_PORT, the variables the compose postgres service consumes.
"""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


class SettingsError(ValueError):
    """A MAPPD_* variable has a value that cannot be used."""


@dataclass(frozen=True, kw_only=True)
class Settings:
    """Runtime configuration; each field is read from MAPPD_<FIELD NAME IN CAPS>."""

    database_url: str = "postgresql://mappd:mappd@127.0.0.1:5432/mappd"
    system_database_url: str = "postgresql://mappd:mappd@127.0.0.1:5432/mappd_dbos"
    gateway_url: str = "http://127.0.0.1:4000/v1"
    # 5000 is taken by macOS (AirPlay); experiment 0 is MLflow's Default, present on a fresh server.
    mlflow_url: str = "http://127.0.0.1:5001"
    mlflow_experiment_id: str = "0"
    app_name: str = "mappd"
    app_version: str = "dev"
    drain_seconds: float = 30.0
    processes_dir: Path = Path("processes")
    world_path: Path = Path("world.yaml")
    gateway_config: Path = Path("deploy/agentgateway/gateway.yaml")
    workflows_payloads: bool = True
    auth: str = "none"
    auth_secret: str = ""
    auth_token_hours: float = 8.0
    auth_header: str = "X-Forwarded-User"
    auth_roles_header: str = "X-Forwarded-Roles"
    auth_cookie_secure: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read settings from `env` (default: the process environment over `.env`).

        Raises:
            SettingsError: a variable is set to an unusable value.
        """
        source = _env_with_dotenv() if env is None else env
        defaults = cls()

        def get(name: str, default: str) -> str:
            return source.get(f"MAPPD_{name}", "") or default

        pg = f"{get('PG_USER', 'mappd')}:{get('PG_PASSWORD', 'mappd')}@127.0.0.1"
        pg = f"{pg}:{get('PG_PORT', '5432')}"
        return cls(
            database_url=get("DATABASE_URL", f"postgresql://{pg}/mappd"),
            system_database_url=get("SYSTEM_DATABASE_URL", f"postgresql://{pg}/mappd_dbos"),
            gateway_url=get("GATEWAY_URL", defaults.gateway_url).rstrip("/"),
            mlflow_url=get("MLFLOW_URL", defaults.mlflow_url).rstrip("/"),
            mlflow_experiment_id=get("MLFLOW_EXPERIMENT_ID", defaults.mlflow_experiment_id),
            app_name=get("APP_NAME", defaults.app_name),
            app_version=get("APP_VERSION", "")
            or _version_file(source.get("MAPPD_APP_VERSION_FILE", ""))
            or defaults.app_version,
            drain_seconds=_positive_number(
                "MAPPD_DRAIN_SECONDS", get("DRAIN_SECONDS", str(defaults.drain_seconds))
            ),
            processes_dir=Path(get("PROCESSES_DIR", str(defaults.processes_dir))),
            world_path=Path(get("WORLD_PATH", str(defaults.world_path))),
            gateway_config=Path(get("GATEWAY_CONFIG", str(defaults.gateway_config))),
            workflows_payloads=_on_off("MAPPD_WORKFLOWS_PAYLOADS", get("WORKFLOWS_PAYLOADS", "on")),
            auth=_auth_mode(get("AUTH", defaults.auth)),
            auth_secret=get("AUTH_SECRET", defaults.auth_secret),
            auth_token_hours=_positive_number(
                "MAPPD_AUTH_TOKEN_HOURS", get("AUTH_TOKEN_HOURS", str(defaults.auth_token_hours))
            ),
            auth_header=get("AUTH_HEADER", defaults.auth_header),
            auth_roles_header=get("AUTH_ROLES_HEADER", defaults.auth_roles_header),
            auth_cookie_secure=_on_off("MAPPD_AUTH_COOKIE_SECURE", get("AUTH_COOKIE_SECURE", "on")),
        )


def _auth_mode(raw: str) -> str:
    """none, jwt, header or a `module.path:factory` import string."""
    if raw in ("none", "jwt", "header") or ":" in raw:
        return raw
    raise SettingsError(
        f"MAPPD_AUTH must be 'none', 'jwt', 'header' or a 'module:factory' import"
        f" string, got {raw!r}"
    )


def _on_off(name: str, raw: str) -> bool:
    if raw not in ("on", "off"):
        raise SettingsError(f"{name} must be on or off, got {raw!r}")
    return raw == "on"


def environment() -> Mapping[str, str]:
    """The process environment over `.env`: what MAPP-D settings read (mail, channels)."""
    return _env_with_dotenv()


def _env_with_dotenv() -> Mapping[str, str]:
    """The process environment over `.env` (MAPPD_ENV_FILE overrides the path).

    Variables already set in the environment always win; a missing file is not
    an error — the defaults still match `docker compose up`.
    """
    path = Path(os.environ.get("MAPPD_ENV_FILE", "") or ".env")
    if not path.is_file():
        return os.environ
    file_vars: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError as e:
        raise SettingsError(f"{path}: cannot read env file: {e}") from None
    for line in lines:
        stripped = line.strip().removeprefix("export ").strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        file_vars[key.strip()] = value.strip().strip('"').strip("'")
    return {**file_vars, **os.environ}


def _positive_number(name: str, raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{name}: expected a positive number, got {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise SettingsError(f"{name}: expected a positive number, got {raw!r}")
    return value


def _version_file(path: str) -> str:
    """The version an image baked in at build time (MAPPD_APP_VERSION_FILE), if set."""
    if not path:
        return ""
    try:
        return Path(path).read_text().strip()
    except OSError as e:
        raise SettingsError(f"MAPPD_APP_VERSION_FILE: cannot read {path}: {e}") from None
