from pathlib import Path

import pytest

from mappd.settings import Settings, SettingsError


def test_defaults_point_at_the_local_stack() -> None:
    s = Settings.from_env({})
    assert s.database_url == "postgresql://mappd:mappd@127.0.0.1:5432/mappd"
    assert s.system_database_url == "postgresql://mappd:mappd@127.0.0.1:5432/mappd_dbos"
    assert s.gateway_url == "http://127.0.0.1:4000/v1"
    assert s.mlflow_url == "http://127.0.0.1:5001"
    assert s.mlflow_experiment_id == "0"
    assert s.app_name == "mappd"
    assert s.app_version == "dev"
    assert s.drain_seconds == 30.0
    assert s.processes_dir == Path("processes")
    assert s.world_path == Path("world.yaml")


def test_every_field_reads_its_variable() -> None:
    s = Settings.from_env(
        {
            "MAPPD_DATABASE_URL": "postgresql://a/app",
            "MAPPD_SYSTEM_DATABASE_URL": "postgresql://a/sys",
            "MAPPD_GATEWAY_URL": "http://gw:4000/v1/",
            "MAPPD_MLFLOW_URL": "http://mlflow:5000/",
            "MAPPD_MLFLOW_EXPERIMENT_ID": "7",
            "MAPPD_APP_NAME": "acme",
            "MAPPD_APP_VERSION": "a1b2c3d",
            "MAPPD_DRAIN_SECONDS": "12.5",
            "MAPPD_PROCESSES_DIR": "/srv/processes",
            "MAPPD_WORLD_PATH": "/srv/world.yaml",
        }
    )
    assert s.database_url == "postgresql://a/app"
    assert s.system_database_url == "postgresql://a/sys"
    assert s.gateway_url == "http://gw:4000/v1", "trailing slash is stripped"
    assert s.mlflow_url == "http://mlflow:5000"
    assert s.mlflow_experiment_id == "7"
    assert s.app_name == "acme"
    assert s.app_version == "a1b2c3d"
    assert s.drain_seconds == 12.5
    assert s.processes_dir == Path("/srv/processes")
    assert s.world_path == Path("/srv/world.yaml")


def test_empty_variable_means_default() -> None:
    # Compose files often pass `MAPPD_GATEWAY_URL: ${GATEWAY_URL:-}`.
    assert Settings.from_env({"MAPPD_GATEWAY_URL": ""}).gateway_url == "http://127.0.0.1:4000/v1"


@pytest.mark.parametrize("raw", ["soon", "0", "-1", "nan", "inf"])
def test_drain_seconds_must_be_a_positive_number(raw: str) -> None:
    with pytest.raises(SettingsError, match="MAPPD_DRAIN_SECONDS"):
        Settings.from_env({"MAPPD_DRAIN_SECONDS": raw})


def test_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPPD_APP_VERSION", "from-env")
    assert Settings.from_env().app_version == "from-env"


def test_version_file_supplies_the_version(tmp_path: Path) -> None:
    version = tmp_path / "VERSION"
    version.write_text("3f2a9c1b7d4e\n")
    assert Settings.from_env({"MAPPD_APP_VERSION_FILE": str(version)}).app_version == "3f2a9c1b7d4e"


def test_explicit_version_wins_over_the_file(tmp_path: Path) -> None:
    version = tmp_path / "VERSION"
    version.write_text("from-file\n")
    env = {"MAPPD_APP_VERSION_FILE": str(version), "MAPPD_APP_VERSION": "v2"}
    assert Settings.from_env(env).app_version == "v2"


def test_missing_version_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="MAPPD_APP_VERSION_FILE"):
        Settings.from_env({"MAPPD_APP_VERSION_FILE": str(tmp_path / "absent")})


def test_gateway_config_from_env() -> None:
    s = Settings.from_env({"MAPPD_GATEWAY_CONFIG": "deploy/agentgateway/gateway.mocks.yaml"})
    assert s.gateway_config == Path("deploy/agentgateway/gateway.mocks.yaml")
    assert Settings.from_env({}).gateway_config == Path("deploy/agentgateway/gateway.yaml")


def test_database_urls_derive_from_pg_vars() -> None:
    s = Settings.from_env(
        {"MAPPD_PG_USER": "alice", "MAPPD_PG_PASSWORD": "secret", "MAPPD_PG_PORT": "15432"}
    )
    assert s.database_url == "postgresql://alice:secret@127.0.0.1:15432/mappd"
    assert s.system_database_url == "postgresql://alice:secret@127.0.0.1:15432/mappd_dbos"


def test_explicit_database_urls_win_over_pg_vars() -> None:
    s = Settings.from_env(
        {
            "MAPPD_PG_USER": "alice",
            "MAPPD_DATABASE_URL": "postgresql://other/db",
            "MAPPD_SYSTEM_DATABASE_URL": "postgresql://other/sys",
        }
    )
    assert s.database_url == "postgresql://other/db"
    assert s.system_database_url == "postgresql://other/sys"


def test_dotenv_file_loads_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\n"
        "MAPPD_APP_NAME=acme   # inline comment\n"
        "export MAPPD_PG_PORT=15433 # another comment\n"
        "MAPPD_PG_PASSWORD='a # b'\n"
    )
    monkeypatch.delenv("MAPPD_APP_NAME", raising=False)
    monkeypatch.delenv("MAPPD_PG_PORT", raising=False)
    monkeypatch.delenv("MAPPD_PG_PASSWORD", raising=False)
    monkeypatch.setenv("MAPPD_ENV_FILE", str(env_file))
    s = Settings.from_env()
    assert s.app_name == "acme"
    assert s.database_url == "postgresql://mappd:a # b@127.0.0.1:15433/mappd"


def test_process_env_beats_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MAPPD_APP_NAME=from-file\n")
    monkeypatch.setenv("MAPPD_ENV_FILE", str(env_file))
    monkeypatch.setenv("MAPPD_APP_NAME", "from-env")
    assert Settings.from_env().app_name == "from-env"


def test_dotenv_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("MAPPD_APP_NAME=cwd-file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MAPPD_ENV_FILE", raising=False)
    monkeypatch.delenv("MAPPD_APP_NAME", raising=False)
    assert Settings.from_env().app_name == "cwd-file"


def test_missing_env_file_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPPD_ENV_FILE", str(tmp_path / "absent"))
    assert Settings.from_env().app_name == "mappd"


def test_explicit_env_does_not_read_dotenv(tmp_path: Path) -> None:
    # An explicit mapping (tests, callers passing os.environ copies) never touches files.
    assert Settings.from_env({}).app_name == "mappd"


@pytest.mark.parametrize(("raw", "expected"), [("", True), ("on", True), ("off", False)])
def test_workflows_payloads_switch(raw: str, expected: bool) -> None:
    assert Settings.from_env({"MAPPD_WORKFLOWS_PAYLOADS": raw}).workflows_payloads is expected


def test_workflows_payloads_rejects_other_values() -> None:
    with pytest.raises(SettingsError, match="MAPPD_WORKFLOWS_PAYLOADS"):
        Settings.from_env({"MAPPD_WORKFLOWS_PAYLOADS": "maybe"})


def test_auth_defaults_to_open() -> None:
    s = Settings.from_env({})
    assert s.auth == "none"
    assert s.auth_secret == ""
    assert s.auth_token_hours == 8.0
    assert s.auth_header == "X-Forwarded-User"
    assert s.auth_roles_header == "X-Forwarded-Roles"
    assert s.auth_cookie_secure is True


def test_auth_modes_and_custom_import_strings() -> None:
    for mode in ("none", "jwt", "header", "mycompany.auth:backend"):
        assert Settings.from_env({"MAPPD_AUTH": mode}).auth == mode


@pytest.mark.parametrize("raw", ["basic", "openid", "JWT"])
def test_auth_rejects_unknown_modes(raw: str) -> None:
    with pytest.raises(SettingsError, match="MAPPD_AUTH"):
        Settings.from_env({"MAPPD_AUTH": raw})


def test_auth_fields_read_their_variables() -> None:
    s = Settings.from_env(
        {
            "MAPPD_AUTH_SECRET": "x" * 40,
            "MAPPD_AUTH_TOKEN_HOURS": "1.5",
            "MAPPD_AUTH_HEADER": "X-User",
            "MAPPD_AUTH_ROLES_HEADER": "X-Roles",
            "MAPPD_AUTH_COOKIE_SECURE": "off",
        }
    )
    assert s.auth_secret == "x" * 40
    assert s.auth_token_hours == 1.5
    assert s.auth_header == "X-User"
    assert s.auth_roles_header == "X-Roles"
    assert s.auth_cookie_secure is False


@pytest.mark.parametrize("raw", ["soon", "0", "-1", "nan", "inf"])
def test_auth_token_hours_must_be_positive_and_finite(raw: str) -> None:
    with pytest.raises(SettingsError, match="MAPPD_AUTH_TOKEN_HOURS"):
        Settings.from_env({"MAPPD_AUTH_TOKEN_HOURS": raw})


def test_auth_cookie_secure_rejects_other_values() -> None:
    with pytest.raises(SettingsError, match="MAPPD_AUTH_COOKIE_SECURE"):
        Settings.from_env({"MAPPD_AUTH_COOKIE_SECURE": "maybe"})
