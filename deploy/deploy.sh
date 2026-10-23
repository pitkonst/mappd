#!/bin/sh
# Deploy one commit to a server: copy its compose files, pull its images, run them.
#   MAPPD_DEPLOY_REGISTRY=ghcr.io/<org> deploy/deploy.sh <ssh-host> <sha>
# Rollback is a deploy of an older SHA.
set -eu

usage="usage: MAPPD_DEPLOY_REGISTRY=ghcr.io/<org> deploy.sh <ssh-host> <sha>"
host=${1:?$usage}
sha_arg=${2:?$usage}
registry=${MAPPD_DEPLOY_REGISTRY:?$usage}
dir=${MAPPD_DEPLOY_DIR:-/opt/mappd}
cd "$(dirname "$0")/.."
sha=$(git rev-parse --short=12 "$sha_arg^{commit}")
compose="docker compose -f compose.yaml -f compose.app.yaml -f compose.deploy.yaml --env-file .env --env-file release.env"

git archive "$sha" compose.yaml compose.app.yaml compose.deploy.yaml deploy/agentgateway deploy/postgres |
    ssh -o BatchMode=yes "$host" "mkdir -p $dir && tar -x -C $dir"
ssh -o BatchMode=yes "$host" "cd $dir && printf 'MAPPD_APP_VERSION=%s\nMAPPD_APP_IMAGE=%s\nMAPPD_MLFLOW_IMAGE=%s\n' $sha $registry/mappd-app:$sha $registry/mappd-mlflow:$sha > release.env"
ssh -o BatchMode=yes "$host" "cd $dir && $compose pull && $compose up -d --wait --remove-orphans"
ssh -o BatchMode=yes "$host" "cd $dir && $compose exec -T app mappd doctor" ||
    echo "deployed $sha, but doctor reports problems (above)" >&2
echo "deployed $sha"
