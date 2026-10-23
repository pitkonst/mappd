# Deploy runbook

One host per environment, immutable images, manual deploys over SSH with
`deploy/deploy.sh` (POSIX sh; needs only `git`, `ssh` and `tar` on the laptop).

## Server setup

Once per host:

1. Docker and the Compose plugin **≥ 2.24** (`docker compose version`; the overlay's
   `!reset` needs it). A `deploy` user in the `docker` group, reachable over SSH by key.
2. `docker login ghcr.io` as that user with a GitHub PAT that has `read:packages` only —
   the server pulls, never pushes.
3. `/opt/mappd/.env`: copy `.env.example`, then fill in the real values — a fresh
   `MAPPD_PG_PASSWORD`, `OPENROUTER_API_KEY`, and the real mail servers
   (`MAPPD_SUPPORT_{IMAP,SMTP}_{HOST,PORT,TLS,USER,PASSWORD}` and the `MAPPD_RFQ_*` set). The
   deploy overlay passes exactly these HOST/PORT/TLS values to `app` and `migrate`; the
   deploy never creates or edits this file.

`/opt/mappd` then holds `.env`, the compose files and `deploy/` configs of the last deployed
SHA, and a `release.env` written by each deploy (`MAPPD_APP_VERSION`, `MAPPD_APP_IMAGE`,
`MAPPD_MLFLOW_IMAGE`). The compose project is the directory name (`mappd`), so volumes and
databases survive across deploys. `MAPPD_DEPLOY_DIR` overrides the directory.

## Reverse proxy (HTTP/2 required)

The app listens on `127.0.0.1:${MAPPD_APP_PORT:-8000}` and speaks plain HTTP/1.1. Put a TLS proxy
that serves **HTTP/2** to browsers in front of it (Caddy does by default, as does nginx with
`listen 443 ssl; http2 on;`). Over HTTP/1.1 a browser allows only six connections per host,
shared by all its tabs. Each visible queue or job page holds one of them for its live updates,
so a seventh request hangs until one of those pages closes. Pages give their stream up while
their tab is hidden, which keeps localhost usable, but HTTP/2 removes the limit. The proxy
must not buffer `text/event-stream` responses: nginx needs `proxy_buffering off;` on
`/api/v1/`, while Caddy needs no setting.

## GitHub setup

Once per repository:

- The `Deploy` workflow is an example: it stays skipped until the *repository variable*
  `DEPLOY_ENABLED` is `true` (Settings → Secrets and variables → Actions → Variables). A
  repository variable, not an environment one — the job-level `if` is read before the job
  attaches its environment.
- GitHub **environments** `staging` and `prod` (Settings → Environments). Each gets the
  *variables* `DEPLOY_HOST` (e.g. `deploy@server`) and `DEPLOY_KNOWN_HOSTS` (the server's
  `ssh-keyscan` output), and the *secret* `DEPLOY_SSH_KEY` (an ed25519 private key accepted by
  the deploy user). Required reviewers on `prod` are optional GitHub configuration.
- Every push to `main` runs the `CI` workflow: the fast checks (pre-commit and the tests
  without Postgres), then `ghcr.io/<org>/mappd-app:<sha12>` and
  `ghcr.io/<org>/mappd-mlflow:<sha12>` are built and pushed — one SHA names a release.

## CI on another forge

CI is plain `uv` commands, so any forge runs the same:

```bash
uv sync --locked --all-groups
uv run pre-commit run --all-files
uv run pytest -m "not pg and not acceptance"
```

On GitLab:

```yaml
check:
  image: python:3.14-slim-trixie
  before_script:
    - apt-get update && apt-get install -y --no-install-recommends git
    - pip install uv==0.9.5
  script:
    - uv sync --locked --all-groups
    - uv run pre-commit run --all-files
    - uv run pytest -m "not pg and not acceptance"
```

(git is needed by `mappd data check-canary` and pre-commit.)

## Deploy

```bash
MAPPD_DEPLOY_REGISTRY=ghcr.io/<org> deploy/deploy.sh deploy@server <sha>
```

or the **Deploy** workflow (Actions → Deploy → environment, sha). What it does:

1. `git archive <sha>` of the compose files and `deploy/` configs → untarred on the server.
2. Writes `release.env` pinning `MAPPD_APP_VERSION` and both image tags to the SHA.
3. `docker compose … pull && up -d --wait --remove-orphans` — migrate runs first via
   `depends_on`; `--wait` holds until every healthcheck is green.
4. `mappd doctor` on the new app — informational: a failure is a warning, not a failed deploy.

`up --wait` failing (unhealthy container, failed migration) exits non-zero.

## Rollback

```bash
MAPPD_DEPLOY_REGISTRY=ghcr.io/<org> deploy/deploy.sh deploy@server <older-sha>
```

A rollback is just a deploy of an older SHA; it works because migrations are expand-only:

- `migrate` sees a database **ahead** of the code and does nothing — the newer schema stays
  and the older code runs on it.
- `doctor` reports the ahead database as ok, so the container healthcheck stays green.
- At startup the app calls `set_latest_application_version` with its own version, so DBOS
  queues the scheduled polls (mail, Telegram) to the rolled-back version again.

**Stranded workflows.** After a deploy, workflows of the old version that are still pending
are *stranded*. The post-deploy `mappd doctor` lists them; recover with:

```bash
ssh deploy@server docker exec mappd-app-1 mappd workflow recover
```

`recover` forks each stranded workflow onto the running version; finished steps are copied
from the journal and only the rest re-runs — a business-keyed action never sends twice. The
same list is at `/workflows?status=stranded`. The target is the version the app runs (its
`MAPPD_APP_VERSION`), so after a rollback that is the old SHA. The container is
`mappd-app-1`: the compose project is the directory name (`mappd`); with another
`MAPPD_DEPLOY_DIR`, use `<dir>-app-1`.

**DBOS upgrade caveat.** A deploy that upgrades the DBOS library itself is one-way: the system
database may migrate forward, and older code may refuse to start on it. Pin DBOS changes to
their own deploy and treat them as no-rollback.

## Status and logs

```bash
ssh deploy@server docker ps --filter label=com.docker.compose.project=mappd
ssh deploy@server docker logs mappd-app-1          # --follow, --since 10m
ssh deploy@server docker exec mappd-app-1 mappd doctor
```

## Staging on mocks

To run staging without real providers or mailboxes, set in `/opt/mappd/.env`:

```
COMPOSE_PROFILES=mocks,dev
MAPPD_GATEWAY_CONFIG=gateway.mocks.yaml
```

(`mocks` alone is not enough: `slow-smtp` depends on the `dev`-profiled `greenmail`.) Then the
same `up -d` brings the mocks up with the rest of the stack.

## Release checklist

Nothing enforces this — it is documented, not automated:

1. `uv run pytest` locally (needs the dev Postgres).
2. `uv run pytest -m acceptance` locally (builds the images; a few minutes).
3. Deploy to staging; run through the app's pages once.
4. Deploy to prod.

## Manual E2E checklist

Run once on any SSH-reachable Docker host:

- [ ] Deploy `<shaA>` → exit 0, doctor ok, `/health` serves A.
- [ ] Commit a dummy expand migration (e.g. `op.execute("CREATE TABLE deploy_probe (id int)")`),
      deploy `<shaB>` → exit 0, `migrate` applied it.
- [ ] Deploy `<shaA>` again (a rollback) → exit 0; `/health` serves A.
- [ ] `ssh deploy@server docker exec mappd-app-1 mappd db migrate --json` reports
      `"ahead": [<B revision>]` and exits 0; `mappd doctor` (the same way) reports the postgres
      check ok ("ahead of this code").
- [ ] The mail poll workflow runs after the rollback (a `mappd.mail_poll` workflow on version A
      dequeues — `docker exec mappd-app-1 mappd workflow list`).
- [ ] A second deploy while one runs → the GitHub concurrency group serializes them.

## Disk housekeeping

Old `mappd-app` and `mappd-mlflow` images pile up on the server; `docker image prune` by hand
when it matters. Postgres, MLflow and named volumes are never touched.
