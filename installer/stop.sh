#!/usr/bin/env bash
set -euo pipefail
cd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v docker >/dev/null 2>&1; then printf '%s\n' 'Docker is not installed.' >&2; exit 1; fi
if [ -n "${DOCKER_CONTEXT:-}" ]; then endpoint="$(docker context inspect "$DOCKER_CONTEXT" --format '{{.Endpoints.docker.Host}}')"
elif [ -n "${DOCKER_HOST:-}" ]; then endpoint="$DOCKER_HOST"
else endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}')"; fi
case "$endpoint" in unix://*|npipe://*) ;; *) printf '%s\n' 'Refusing to stop containers on a remote Docker endpoint.' >&2; exit 1;; esac
if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then COMPOSE=(docker-compose)
else printf '%s\n' 'Docker Compose v2 is required.' >&2; exit 1; fi
compose() { "${COMPOSE[@]}" -p forgeflow-local -f compose.yaml "$@"; }
running="$(compose ps --status running --services)"
if printf '%s\n' "$running" | grep -qx api; then compose stop -t -1 api; fi
if printf '%s\n' "$running" | grep -qx supervisor; then
  compose exec -T supervisor python -m forgeflow.local_workbench.runtime quiesce
  idle=false
  for ((attempt=0; attempt<150; attempt++)); do
    if compose exec -T supervisor python -m forgeflow.local_workbench.runtime doctor --require-idle; then idle=true; break; fi
    sleep 2
  done
  if ! "$idle"; then printf '%s\n' 'Background work remains active. Admission is paused; workers were left running to finish. Retry later.' >&2; exit 1; fi
fi
compose stop -t -1
printf '%s\n' 'ForgeFlow stopped. Configuration, databases, documents and local volumes are preserved.'
