#!/usr/bin/env bash
# Restores a snapshot into a new database with no worker attached to it.
set -euo pipefail
cd "$(dirname "$0")/.."
dc() { docker compose -f compose.server.yaml "$@"; }
restore_db="cu_clubs_restore_check_$(date -u +%Y%m%d%H%M%S)"
dc exec -T --interactive=false db createdb -U cu_clubs "$restore_db"
trap 'dc exec -T --interactive=false db dropdb -U cu_clubs "$restore_db"' EXIT
backup_name=$(dc exec -T --interactive=false worker python -c \
  'from pathlib import Path; print(sorted(Path("/backups").glob("cu-clubs-*.dump"))[-1].name)')
dc exec -T --interactive=false worker cat "/backups/$backup_name" \
  | dc exec -T db pg_restore -U cu_clubs --no-owner --no-acl --exit-on-error \
      --dbname "$restore_db"
dc exec -T --interactive=false db psql -U cu_clubs -d "$restore_db" -c \
  "SELECT (SELECT count(*) FROM clubs) AS clubs, (SELECT count(*) FROM global_admins) AS admins, (SELECT count(*) FROM memberships WHERE role='leader') AS leaders;"
