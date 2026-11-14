#!/usr/bin/env bash
# Run the signed/reviewed local bundle. No host Python, Node or package installs.
set -euo pipefail
cd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
configure=false
no_open=false
for arg in "$@"; do
  case "$arg" in
    --configure) configure=true ;;
    --no-open) no_open=true ;;
    *) printf '%s\n' 'Usage: start.sh [--configure] [--no-open]' >&2; exit 2 ;;
  esac
done
if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' 'Docker is required. Install Docker Desktop for macOS/Windows or Docker Engine + Compose for Linux:' 'https://docs.docker.com/get-started/get-docker/' >&2
  exit 1
fi
if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then COMPOSE=(docker-compose)
else printf '%s\n' 'Docker Compose v2 is required: https://docs.docker.com/compose/install/' >&2; exit 1; fi
# An existing remote Docker context must never receive this local installation.
if [ -n "${DOCKER_CONTEXT:-}" ]; then endpoint="$(docker context inspect "$DOCKER_CONTEXT" --format '{{.Endpoints.docker.Host}}')"
elif [ -n "${DOCKER_HOST:-}" ]; then endpoint="$DOCKER_HOST"
else endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}')"; fi
case "$endpoint" in unix://*|npipe://*) ;; *) printf '%s\n' 'Select a local Docker context first; remote Docker endpoints are not supported.' >&2; exit 1;; esac
if ! docker info --format '{{.OSType}}' 2>/dev/null | grep -qx linux; then
  printf '%s\n' 'Start Docker and select Linux containers, then run this launcher again.' >&2; exit 1
fi
COMPOSE_EXTRA=()
compose() { "${COMPOSE[@]}" -p forgeflow-local -f compose.yaml ${COMPOSE_EXTRA[@]+"${COMPOSE_EXTRA[@]}"} "$@"; }
open_url() {
  if "$no_open"; then printf '%s\n' "$1"; return; fi
  if command -v open >/dev/null 2>&1; then open "$1"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 || printf '%s\n' "$1"
  else printf '%s\n' "$1"; fi
}
running="$(compose ps --status running --services)"
if ! "$configure" && printf '%s\n' "$running" | grep -qx api; then
  compose exec -T api python -m forgeflow.local_workbench.runtime doctor
  printf '%s\n' 'ForgeFlow is already running. To install a new version, safely stop the old installation first.'
  open_url 'http://127.0.0.1:17800/app/'
  exit 0
fi
quiesce() {
  running="$(compose ps --status running --services)"
  if printf '%s\n' "$running" | grep -qx api; then
    printf '%s\n' 'Pausing new browser requests; waiting for active requests to finish.'
    compose stop -t -1 api
  fi
  if printf '%s\n' "$running" | grep -qx supervisor; then
    compose exec -T supervisor python -m forgeflow.local_workbench.runtime quiesce
    idle=false
    for ((attempt=0; attempt<150; attempt++)); do
      if compose exec -T supervisor python -m forgeflow.local_workbench.runtime doctor --require-idle; then idle=true; break; fi
      sleep 2
    done
    if ! "$idle"; then printf '%s\n' 'Background work is still active. Admission remains paused; no worker was killed. Try stopping again when it completes.' >&2; exit 1; fi
  fi
  compose stop -t -1 supervisor docmind
}
if "$configure"; then quiesce; fi
if ! compose pull setup odoo-init; then
  if [ ! -f compose.source.yaml ] || [ ! -d source ]; then
    printf '%s\n' 'Image download failed and this bundle has no source fallback. Download the complete release bundle.' >&2; exit 1
  fi
  printf '%s\n' 'Prebuilt images are unavailable. Building the bundled source locally; base images still require internet access.'
  COMPOSE_EXTRA=(-f compose.source.yaml)
  compose build setup odoo-init
fi
compose up -d --force-recreate setup
ready=false
for ((attempt=0; attempt<60; attempt++)); do
  if setup_url="$(compose exec -T setup python -m forgeflow.local_workbench.setup url 2>/dev/null)"; then ready=true; break; fi
  sleep 2
done
if ! "$ready"; then printf '%s\n' 'Setup did not become ready. Inspect service status with docker compose -p forgeflow-local -f compose.yaml ps.' >&2; exit 1; fi
if "$configure" || ! compose exec -T setup python -m forgeflow.local_workbench.setup status >/dev/null; then
  printf '%s\n' 'Complete the local setup page. The private one-time link expires after 30 minutes.'
  open_url "$setup_url"
  saved=false
  for ((attempt=0; attempt<900; attempt++)); do
    if compose exec -T setup python -m forgeflow.local_workbench.setup status --saved >/dev/null 2>&1; then saved=true; break; fi
    sleep 2
  done
  if ! "$saved"; then compose stop setup; printf '%s\n' 'Setup was not completed. Run the launcher again; existing data remains untouched.' >&2; exit 1; fi
fi
unset setup_url
printf '%s\n' 'Starting ForgeFlow. First start downloads containers and initializes the local demonstration; it can take several minutes.'
if "$configure"; then
  compose up -d --wait --wait-timeout 900 --force-recreate api supervisor docmind
else
  compose up -d --wait --wait-timeout 900
fi
compose exec -T supervisor python -m forgeflow.local_workbench.runtime resume-admission
compose stop setup
printf '%s\n' 'ForgeFlow is ready: http://127.0.0.1:17800/app/' 'Use the invite code shown by setup. Stop scripts preserve all local data.'
open_url 'http://127.0.0.1:17800/app/'
