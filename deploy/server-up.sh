#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p .cache
status_file=.cache/deploy.status
rm -f "$status_file"
trap 'result=$?; printf "%s\n" "$result" > "$status_file"' EXIT

dc() { docker compose -f compose.server.yaml "$@"; }

dc config --quiet
dc build
dc up -d --wait --wait-timeout 90 db
dc run --rm --no-deps api cu-clubs migrate
dc run --rm --no-deps api cu-clubs bootstrap
dc up -d --wait --wait-timeout 180 api worker
curl --fail --silent --show-error http://127.0.0.1:8310/health/ready
dc ps
